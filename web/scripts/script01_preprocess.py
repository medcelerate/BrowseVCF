#!/usr/bin/env python
import os
import sys
import gzip
import platform
import argparse
from subprocess import Popen, PIPE
from datetime import datetime

# Cross-platform stuff...let's first figure out what we're running on
current_os = platform.system()

# Agnostic paths based on platform
WIN_PLATFORM_NONFREE = False

# GNU default path for Ubuntu/Debian
#TODO: Load paths dynamically, or ship the GNU binaries locally as well
TOOLPATH = "/usr/local/bin/"
if 'windows' in current_os.lower():
  WIN_PLATFORM_NONFREE = True
  TOOLPATH = "./win_tools/"

if current_os.lower() == 'darwin':
    os.environ['PYTHONPATH'] = '%s/osx_libs:$PYTHONPATH' % os.getcwd()

import wormtable as wt

################################################################################
# This script allows the user to convert the input .vcf or .vcf.gz file into a
# pre-processed vcf file that can be easily accepted by Wormtable. It also
# generates the global schema file and edits it in place.
################################################################################


def parse_args():
  """
  Parse the input arguments.
  """

  parser = argparse.ArgumentParser()
  parser.add_argument('-i', dest = 'inp_file', required = True,
                      help = 'input file [.vcf|.vcf.gz]')
  parser.add_argument('-o', dest = 'out_folder', required = True,
                      help = 'output folder [will be created]')
  args = parser.parse_args()
  return args

def check_input_file(file_name):
  """
  Make sure that the input file's path is properly defined and that is a
  compressed file.
  """

  if not os.path.exists(file_name):
    sys.stderr.write("\nFile named '" + file_name + "' does not exist.\n")
    sys.exit(1)
  sys.stderr.write("Input file checked.\n")
  return file_name

def check_output_file(folder_name):
  """
  If folder_name does not already exist, create it, otherwise raise an error.
  """

  if os.path.exists(os.path.normpath(folder_name)):
    sys.stderr.write("\nFolder named '" + folder_name + "' already exists.\n")
  else:
    os.makedirs(os.path.normpath(folder_name))
    sys.stderr.write("Created %s\n" % os.path.normpath(folder_name))
  sys.stderr.write("Output directory checked.\n")
  return folder_name

def handle_vep_line(line, vep_symbol):
  """
  Divide the VEP header line in multiple 'CSQ_<subfield>' or 'ANN_<subfield>'
  header lines.
  """

  new_line = ''
  sub_fields = list()
  line_s = line.strip('">\r\n').split('Format: ')[1].split('|')
  for field in line_s:
    sub_fields.append(field)
    new_line += '##INFO=<ID=' + vep_symbol + '_' + field + \
                ',Number=.,Type=String,' + \
                'Description="VEP annotation field ' + field + '">\n'
  return sub_fields, new_line

def substitute_dots(line):
  """
  Whenever a value in a subfield of the INFO field is '.', replace it with a
  'nan'.
  """
  try:
      line_s = line.strip('\r\n').split('\t')
      info = {(item.split('=')[0] if item.find('=')!= -1
    	else item):(item.split('=')[1] if item.find('=')!= -1
    	else 'nan') for item in line_s[7].split(';')}

      for key in info:
        values = list()
        for val in info[key].split(','):
          if val == '.':
            values.append('nan')
          else:
            values.append(val)
        info[key] = ','.join(values)
      new_info = ''
      for k in info:
        new_info += k + '=' + info[k] + ';'
      new_info = new_info[:-1]
      new_line = '\t'.join([line_s[0], line_s[1], line_s[2], line_s[3], line_s[4],
                            line_s[5], line_s[6], new_info]) + '\t' + \
                 '\t'.join(line_s[8:]) + '\n'
      return new_line

  except:
      #this VCF is mangled, or it isn't a VCF
      raise #return to caller
      #return "This file is not properly formatted. Please check the \
      #        documentation for the VCF format!"

def split_vep_field(sub_fields, line, vep_symbol):
  """
  Split the VEP field (if present) in single fields named 'CSQ_<subfield>' or
  'ANN_<subfield>'. All annotations will be reported in one line. Fields with
  >1 value will be comma-separated.
  """

  # all_values will contain values from all annotations, in the same order as
  # they are written in the .vcf file and in the same order as sub_fields
  # ex: [['G','G'], ['ENST00000456328','ENST00000541675'], ['YES',''], ...]
  all_values = [[] for x in xrange(len(sub_fields))]
  line_s = line.strip('\r\n').split('\t')
  info = dict(item.split('=') for item in line_s[7].split(';'))
  try:
    annotations = info[vep_symbol].split(',')
    for annot in annotations:
      vep = annot.split('|')
      for x in xrange(len(sub_fields)):
        all_values[x].append(vep[x])
    new_vep = ''
    for x in xrange(len(sub_fields)):
      new_vep += vep_symbol + '_' + sub_fields[x] + '=' + ','.join(all_values[x]) + ';'
    new_vep = new_vep.strip(';')
    spl = line.split(vep_symbol + '=')
    line_until_vep = spl[0]
    fi = spl[1].find(';')
    if fi == -1:  # special case: there are no other fields after 'CSQ' or 'ANN'
      fi = spl[1].find('\t')
    line_after_vep = spl[1][fi:]
    new_line = line_until_vep + new_vep + line_after_vep
  except KeyError:  # there is no 'CSQ' or 'ANN' field
    new_line = line
  return new_line

def parse_inp_file(inp_file, out_folder):
  """
  Parse the input file:
   - split the VEP field (either called 'CSQ' or 'ANN') in subfields
   - substitute any '.' value with 'nan'
   - store all fields in FORMAT (will be used by 'edit_global_schema_in_place')
  Print out sample names at the end, useful by web services.
  """

  samples = []
  NOFILTERB = False

  try:
    inp = gzip.open(inp_file, 'rb')
    inp.read(2) #will fail if not gzipped
    inp.seek(0) #seek back
  except:
    inp = open(inp_file, 'rU')

  out_file = out_folder + '/pre_processed_inp_file.vcf.gz'

  if WIN_PLATFORM_NONFREE:
    out_file = out_folder + "\\pre_processed_inp_file.vcf.gz"

  #write straight out to gzip
  out = gzip.open(out_file, 'wb')

  format_fields = set()
  for line in inp:
    if line.startswith('#'):
      # process the header VEP line
      if line.startswith('##INFO=<ID=CSQ,'):
        vep_symbol = 'CSQ'
        sub_fields, line = handle_vep_line(line, vep_symbol)
      elif line.startswith('##INFO=<ID=ANN,'):
        vep_symbol = 'ANN'
        sub_fields, line = handle_vep_line(line, vep_symbol)
      # collect FORMAT fields in the set format_fields
      if line.startswith('##FORMAT=<ID='):
        format_fields.add(line.split('=', 2)[2].split(',')[0])
      # collect sample names
      elif line[1] != '#':  # sample line doesn't have '##' at start
        try:
          tkns = line[1:].strip().split('\t')
          # everything after 'FORMAT' is a sample. Need this field for script 02
          samples = tkns[tkns.index('FORMAT')+1:]
        except:
          sys.stderr.write('NOFILTERB => VCF does not contain sample ' +
                           'genotypes. Filter B cannot run...\n')
          NOFILTERB = True
    else:
      # substitute '.' values with 'nan'
      line = substitute_dots(line)
      # split the VEP INFO field in several subfields (if 'CSQ' or 'ANN' exists)
      if 'sub_fields' in locals():
        line = split_vep_field(sub_fields, line, vep_symbol)
    out.write(line)

  inp.close()
  out.close()
  # return sample names with file name, and whether filter b can be activated
  return (out_file, samples, NOFILTERB, format_fields)

def create_general_schema(out_folder):
  """
  Create the general .xml schema which will be used as template for the creation
  of several other schemas (one for each field in the .vcf file).
  """

  schema_file = ""
  preproc_file = out_folder + '/pre_processed_inp_file.vcf.gz'
  if WIN_PLATFORM_NONFREE:
    schema_file = out_folder
    schema_file = schema_file.replace('\\', '\\\\')
    schema_file += '\\\\schema.xml'
    preproc_file = preproc_file.replace('\\', '\\\\')
    preproc_file = preproc_file.replace('/', '\\\\')
  else:
    schema_file = out_folder + '/schema.xml'
  runargs = '-q -g ' + preproc_file + ' ' + schema_file
  # call vcf2wt as a library function
  wt.vcf2wt_main(runargs.split())
  return schema_file

def edit_global_schema_in_place(schema_file, format_fields):
  """
  Store all the content of schema_file in memory, then replace all instances of
  'var(1)' with 'var(2)' for REF, ALT, INFO and FORMAT fields. Finally, re-open
  the file in write mode and write all lines.

  This step allows long string fields (up to 64k characters) to be properly
  represented by wormtable, whereas all other string-based fields will be
  represented with less bytes, to save disk space.

  Also, for missing values ('nan') to be properly recognised as floats, all the
  integer INFO fields are converted to float INFO fields.
  """

  all_lines = list()
  f = open(schema_file, 'r')
  for line in f:
    if 'column description' in line:
      if 'name="REF"' in line:
        line = line.replace('var(1)', 'var(2)')
      elif 'name="ALT"' in line:
        line = line.replace('var(1)', 'var(2)')
      # change element size and type to INFO fields too
      elif 'name="INFO.' in line:
        line = line.replace('var(1)', 'var(2)')
        line = line.replace('element_type="int"', 'element_type="float"')
        if 'element_type="uint"' in line:
          line = line.replace('element_type="uint"', 'element_type="float"'
                              ).replace('element_size="1"', 'element_size="4"')
      # change element size and type to FORMAT field too (they might be long)
      elif line.split('name="')[1].split('"')[0].rsplit('.')[-1] in format_fields:
        line = line.replace('var(1)', 'var(2)')
        line = line.replace('element_type="int"', 'element_type="float"')
        if 'element_type="uint"' in line:
          line = line.replace('element_type="uint"', 'element_type="float"'
                              ).replace('element_size="1"', 'element_size="4"')
    all_lines.append(line)
  f.close()
  f = open(schema_file, 'w')
  f.writelines(all_lines)
  f.close()
  return

def script01_api_call(i_file, o_folder):
  """
  API call for web-based and other front-end services, to avoid a system call
  and a new Python process.
  """

  t1 = datetime.now()
  if WIN_PLATFORM_NONFREE:
      sys.stderr.write("Warning! You are using Windows. Fixing tool paths.\n")
  inp_file = check_input_file(i_file)
  out_folder = check_output_file(o_folder)
  (out_file, samples, NOFILTERB, format_fields) = parse_inp_file(inp_file, 
                                                                 out_folder)
  sys.stderr.write("Input file parsed.\n")
  schema_file = create_general_schema(out_folder)
  edit_global_schema_in_place(schema_file, format_fields)
  sys.stderr.write("General schema created and edited.\n")
  t2 = datetime.now()
  sys.stderr.write('%s\n' % str(t2 - t1))
  # return sample names to the calling function and whether we can run filter B
  return (samples, NOFILTERB)

def main():
  """
  Main function.
  """

  args = parse_args()
  script01_api_call(args.inp_file, args.out_folder)

if __name__ == '__main__':
  main()
