#!/usr/bin/python

# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Pipeline to convert variant data from BigQuery to TensorFlow Example protos.

For any samples without corresponding metadata, values indicating
NA will be used instead for the metadata.

USAGE:
  python -m trainer.preprocess_data \
    --setup_file ./setup.py \
    --project ${PROJECT_ID} \
    --metadata preprocess/1000_genomes_metadata.jinja \
    --input preprocess/1000_genomes_phase3_b37_limit10.jinja \
    --output ${BUCKET}/1000-genomes
"""

import datetime
import logging
import os

import apache_beam as beam
from apache_beam.io import tfrecordio
from apache_beam.io.filesystem import CompressionTypes
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.options.pipeline_options import WorkerOptions
from jinja2 import Template

import trainer.ancestry_metadata_encoder as metadata_encoder
import trainer.feature_encoder as encoder
import trainer.util as util
import trainer.variant_encoder as variant_encoder


# Jinja template replacements to decouple column names from the source
# tables from the dictionart keys used in this pipeline.

METADATA_QUERY_REPLACEMENTS = {
    'KEY_COLUMN': encoder.KEY_COLUMN,
    'POPULATION_COLUMN': metadata_encoder.POPULATION_COLUMN,
    'SUPER_POPULATION_COLUMN': metadata_encoder.SUPER_POPULATION_COLUMN,
    'GENDER_COLUMN': metadata_encoder.GENDER_COLUMN,
}

DATA_QUERY_REPLACEMENTS = {
    'KEY_COLUMN': encoder.KEY_COLUMN,
    'CONTIG_COLUMN': encoder.CONTIG_COLUMN,
    'START_COLUMN': encoder.START_COLUMN,
    'END_COLUMN': encoder.END_COLUMN,
    'REF_COLUMN': encoder.REF_COLUMN,
    'ALT_COLUMN': encoder.ALT_COLUMN,
    'ALT_NUM_COLUMN': encoder.ALT_NUM_COLUMN,
    'FIRST_ALLELE_COLUMN': encoder.FIRST_ALLELE_COLUMN,
    'SECOND_ALLELE_COLUMN': encoder.SECOND_ALLELE_COLUMN
}


def variants_to_examples(input_data, samples_metadata, sample_to_example_fn):
  """Converts variants to TensorFlow Example protos.

  Args:
    input_data: variant call dictionary objects with keys from
      DATA_QUERY_REPLACEMENTS
    samples_metadata: metadata dictionary objects with keys from
      METADATA_QUERY_REPLACEMENTS
    sample_to_example_fn: the feature encoder strategy to use to
      convert the source data into TensorFlow Example protos.

  Returns:
    TensorFlow Example protos.
  """
  variant_kvs = input_data | 'BucketVariants' >> beam.Map(
      lambda row: (row[encoder.KEY_COLUMN], row))

  sample_variant_kvs = variant_kvs | 'GroupBySample' >> beam.GroupByKey()

  examples = (
      sample_variant_kvs
      | 'SamplesToExamples' >> beam.Map(
          lambda (key, vals), samples_metadata: sample_to_example_fn(
              key, vals, samples_metadata),
          beam.pvalue.AsSingleton(samples_metadata)))

  return examples


class PreprocessOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser):
    parser.add_argument(
        '--output',
        required=True,
        help='Output directory to which to write results.')
    parser.add_argument(
        '--input',
        required=True,
        help='Jinja file holding the query for the sample data.')
    parser.add_argument(
        '--metadata',
        required=True,
        help='Jinja file holding the query for the sample metadata.')
    parser.add_argument(
        '--hethom_words',
        dest='add_hethom',
        action='store_true',
        help='Add variant heterozygous/homozygous "word".')
    parser.add_argument(
        '--no_hethom_words',
        dest='add_hethom',
        action='store_false',
        help='Do not add variant heterozygous/homozygous "word".')
    parser.set_defaults(add_hethom=True)
    parser.add_argument(
        '--bin_size',
        type=int,
        help='The number of contiguous base pairs to use for each "bin". '
        'This parameter enables the placement of variant "words" into '
        'smaller genomic region features (as opposed to the default '
        'feature-per-chromosome) ')


def run(argv=None):
  """Runs the variant preprocess pipeline.

  Args:
    argv: Pipeline options as a list of arguments.
  """
  pipeline_options = PipelineOptions(flags=argv)
  preprocess_options = pipeline_options.view_as(PreprocessOptions)
  cloud_options = pipeline_options.view_as(GoogleCloudOptions)
  output_dir = os.path.join(preprocess_options.output,
                            datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
  pipeline_options.view_as(SetupOptions).save_main_session = True
  pipeline_options.view_as(
      WorkerOptions).autoscaling_algorithm = 'THROUGHPUT_BASED'
  cloud_options.staging_location = os.path.join(output_dir, 'tmp', 'staging')
  cloud_options.temp_location = os.path.join(output_dir, 'tmp')
  cloud_options.job_name = 'preprocess-varianteatures-%s' % (
      datetime.datetime.now().strftime('%y%m%d-%H%M%S'))

  metadata_query = str(
      Template(open(preprocess_options.metadata, 'r').read()).render(
          METADATA_QUERY_REPLACEMENTS))
  logging.info('metadata query : %s', metadata_query)

  data_query = str(
      Template(open(preprocess_options.input, 'r').read()).render(
          DATA_QUERY_REPLACEMENTS))
  logging.info('data query : %s', data_query)

  # Assemble the strategies to be used to convert the raw data to features.
  variant_to_feature_name_fn = variant_encoder.variant_to_contig_feature_name
  if preprocess_options.bin_size is not None:
    variant_to_feature_name_fn = variant_encoder.build_variant_to_binned_feature_name(
        bin_size=preprocess_options.bin_size)

  variants_to_features_fn = variant_encoder.build_variants_to_features(
      variant_to_feature_name_fn=variant_to_feature_name_fn,
      variant_to_words_fn=variant_encoder.build_variant_to_words(
          add_hethom=preprocess_options.add_hethom))

  sample_to_example_fn = encoder.build_sample_to_example(
      metadata_to_features_fn=metadata_encoder.metadata_to_ancestry_features,
      variants_to_features_fn=variants_to_features_fn)

  with beam.Pipeline(options=pipeline_options) as p:
    # Gather our sample metadata into a python dictionary.
    samples_metadata = (
        p
        | 'ReadSampleMetadata' >> beam.io.Read(
            beam.io.BigQuerySource(query=metadata_query, use_standard_sql=True))
        | 'TableToDictionary' >> beam.CombineGlobally(
            util.TableToDictCombineFn(key_column=encoder.KEY_COLUMN)))

    # Read the table rows into a PCollection.
    rows = p | 'ReadVariants' >> beam.io.Read(
        beam.io.BigQuerySource(query=data_query, use_standard_sql=True))

    # Convert the data into TensorFlow Example Protocol Buffers.
    examples = variants_to_examples(
        rows, samples_metadata, sample_to_example_fn=sample_to_example_fn)

    # Write the serialized compressed protocol buffers to Cloud Storage.
    _ = (examples
         | 'EncodeExamples' >> beam.Map(
             lambda example: example.SerializeToString())
         | 'WriteExamples' >> tfrecordio.WriteToTFRecord(
             file_path_prefix=os.path.join(output_dir, 'examples'),
             compression_type=CompressionTypes.GZIP,
             file_name_suffix='.tfrecord.gz'))


if __name__ == '__main__':
  run()
