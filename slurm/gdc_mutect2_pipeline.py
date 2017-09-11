'''
Main wrapper script for MuTect2 pipeline
'''
import os
import time
import argparse
import logging
import sys
import uuid
import tempfile
import utils.s3
import utils.pipeline
import datetime
import socket
import json
import glob
import postgres.status
import postgres.utils
import postgres.mixins

from sqlalchemy.exc import NoSuchTableError

def is_nat(x):
    '''
    Checks that a value is a natural number.
    '''
    if int(x) > 0:
        return int(x)
    raise argparse.ArgumentTypeError('%s must be positive, non-zero' % x)

def get_args():
    '''
    Loads the parser
    '''
    # Main parser
    parser = argparse.ArgumentParser(description="MuTect2 pipeline")
    # Args
    required = parser.add_argument_group("Required input parameters")
    # Metadata from input table
    required.add_argument("--case_id", default=None, help="Case ID, internal production id.")
    required.add_argument("--tumor_gdc_id", default=None, help="Tumor GDC ID, GDC portal id.")
    required.add_argument("--normal_gdc_id", default=None, help="Normal GDC ID, GDC portal id.")
    required.add_argument("--normal_filesize", default=None, help="Filesize, filesize of normal bam input file.")
    required.add_argument("--tumor_s3_url", default=None, help="S3_URL, s3 url of the tumor input.")
    required.add_argument("--t_s3_profile", required=True, help="S3 profile name for project tenant.")
    required.add_argument("--t_s3_endpoint", required=True, help="S3 endpoint url for project tenant.")
    required.add_argument("--normal_s3_url", default=None, help="S3_URL, s3 url of the normal input.")
    required.add_argument("--n_s3_profile", required=True, help="S3 profile name for project tenant.")
    required.add_argument("--n_s3_endpoint", required=True, help="S3 endpoint url for project tenant.")
    # Parameters for pipeline
    required.add_argument("--pipeline", choices=['vc', 'pon', 'tumor_only'], help="Calling mutect2 on tumor normal pair or creating pon on normal", required=True)
    required.add_argument("--basedir", default="/mnt/SCRATCH/", help="Base directory for computations.")
    required.add_argument("--refdir", required=True, help="Path to reference directory.")
    required.add_argument("--cwl", required=True, help="Path to CWL workflow yaml.")
    required.add_argument("--sort", required=True, help="Path to Picard sortvcf CWL tool.")
    required.add_argument("--s3dir", default="s3://", help="S3bin for uploading output files.")
    required.add_argument("--s3_profile", required=True, help="S3 profile name for project tenant.")
    required.add_argument("--s3_endpoint", required=True, help="S3 endpoint url for project tenant.")
    # Parameters for parallelization
    required.add_argument("--block", type=is_nat, default=30000000, help="Parallel block size.")
    required.add_argument('--java_heap', required=True, help='Java heap memory limit.')
    required.add_argument('--thread_count', type=is_nat, default=8, help='Threads count.')

    return parser.parse_args()

def run_pipeline(args, statusclass, metricsclass):
    '''
    Executes the CWL pipeline and record status/metrics tables
    '''
    if not os.path.isdir(args.basedir):
        raise Exception("Could not find path to base directory: %s" %args.basedir)
    # Generate a uuid
    output_id = uuid.uuid4()
    output_vcf = "{0}.mutect2.{1}.vcf.gz".format(str(output_id), args.pipeline)
    # Get hostname
    hostname = socket.gethostname()
    # Get datetime start
    datetime_start = str(datetime.datetime.now())
    # Create directory structure
    jobdir = tempfile.mkdtemp(prefix="%s_%s_" % (args.pipeline, str(output_id)), dir=args.basedir)
    workdir = tempfile.mkdtemp(prefix="workdir_", dir=jobdir)
    inputdir = tempfile.mkdtemp(prefix="input_", dir=jobdir)
    resultdir = tempfile.mkdtemp(prefix="result_", dir=jobdir)
    jsondir = tempfile.mkdtemp(prefix="input_json_", dir=workdir)
    refdir = args.refdir
    # Setup logger
    log_file = os.path.join(resultdir, "%s.%s.cwl.log" % (args.pipeline, str(output_id)))
    logger = utils.pipeline.setup_logging(logging.INFO, str(output_id), log_file)
    # Logging inputs
    logger.info("pipeline: mutect2 %s" % (args.pipeline))
    logger.info("hostname: %s" % (hostname))
    logger.info("case_id: %s" % (args.case_id))
    logger.info("tumor_gdc_id: %s" % (args.tumor_gdc_id))
    logger.info("normal_gdc_id: %s" % (args.normal_gdc_id))
    logger.info("normal_filesize: %s" % (args.normal_filesize))
    logger.info("datetime_start: %s" % (datetime_start))
    # Setup start point
    cwl_start = time.time()
    # Getting refs
    logger.info("getting resources")
    reference_data        = utils.pipeline.load_reference_json()
    reference_fasta_path  = os.path.join(refdir, reference_data["reference_fasta"])
    reference_fasta_fai   = os.path.join(refdir, reference_data["reference_fasta_index"])
    reference_fasta_dict  = os.path.join(refdir, reference_data["reference_fasta_dict"])
    known_snp_vcf_path    = os.path.join(refdir, reference_data["known_snp_vcf"])
    cosmic_path           = os.path.join(refdir, reference_data["cosmic_path"])
    pon_path              = os.path.join(refdir, reference_data["pon_path"])
    duscb                 = reference_data["duscb"]
    contEst               = reference_data["contEst"]
    postgres_config       = os.path.join(refdir, reference_data["pg_config"])
    # Logging pipeline info
    cwl_version    = reference_data["cwl_version"]
    docker_version = reference_data["docker_version"]
    logger.info("cwl_version: %s" % (cwl_version))
    logger.info("docker_version: %s" % (docker_version))
    # Download input
    if args.pipeline == 'pon':
        normal_bam = os.path.join(inputdir, os.path.basename(args.normal_s3_url))
        normal_download_cmd = utils.s3.aws_s3_get(logger, args.normal_s3_url, inputdir,
                                                 args.n_s3_profile, args.n_s3_endpoint, recursive=False)
        normal_download_exit_code = utils.pipeline.run_command(normal_download_cmd, logger)
        download_end_time = time.time()
        download_time = download_end_time - cwl_start
        #if not (normal_download_exit_code != 0 or str(utils.pipeline.get_file_size(normal_bam)) != args.normal_filesize):
        if not normal_download_exit_code != 0:
            logger.info("Download input %s successfully. Normal bam is %s." % (args.normal_gdc_id, normal_bam))
        else:
            cwl_elapsed = download_time
            datetime_end = str(datetime.datetime.now())
            engine = postgres.utils.get_db_engine(postgres_config)
            postgres.utils.set_download_error(normal_download_exit_code, logger, engine,
                                              args.case_id, args.tumor_gdc_id, args.normal_gdc_id, output_id,
                                              datetime_start, datetime_end,
                                              hostname, cwl_version, docker_version,
                                              download_time, cwl_elapsed, statusclass, metricsclass)
            # Exit
            sys.exit(normal_download_exit_code)
        # Build index
        normal_bam_index_cmd = ['samtools', 'index', normal_bam]
        index_exit = utils.pipeline.run_command(normal_bam_index_cmd, logger)
        if index_exit != 0:
            logger.info("Failed to build bam index.")
            sys.exit(index_exit)
        else:
            normal_bam_index = utils.pipeline.get_index(logger, inputdir, normal_bam)
        # Create input json
        input_json_list = []
        for i, block in enumerate(utils.pipeline.fai_chunk(reference_fasta_fai, args.block)):
            input_json_file = os.path.join(jsondir, '{0}.{4}.{1}.{2}.{3}.mutect2.pon.inputs.json'.format(str(output_id), block[0], block[1], block[2], i))
            input_json_data = {
            "java_heap": args.java_heap,
            "ref": {"class": "File", "path": reference_fasta_path},
            "region": "{0}:{1}-{2}".format(block[0], block[1], block[2]),
            "normal_bam": {"class": "File", "path": normal_bam},
            "cosmic": {"class": "File", "path": cosmic_path},
            "dbsnp": {"class": "File", "path": known_snp_vcf_path},
            "cont": contEst,
            "output_name": '{}_{}_{}.pon.vcf.gz'.format(block[0], block[1], block[2]),
            "duscb": duscb
            }
            with open(input_json_file, 'wt') as o:
                json.dump(input_json_data, o, indent=4)
            input_json_list.append(input_json_file)
        logger.info("Preparing input json")
    elif args.pipeline == 'vc':
        # Download input
        normal_bam = os.path.join(inputdir, os.path.basename(args.normal_s3_url))
        normal_download_cmd = " ".join(utils.s3.aws_s3_get(logger, args.normal_s3_url, inputdir,
                                                 args.n_s3_profile, args.n_s3_endpoint, recursive=False))
        tumor_bam = os.path.join(inputdir, os.path.basename(args.tumor_s3_url))
        tumor_download_cmd = " ".join(utils.s3.aws_s3_get(logger, args.tumor_s3_url, inputdir,
                                                 args.t_s3_profile, args.t_s3_endpoint, recursive=False))
        download_cmd = [normal_download_cmd, tumor_download_cmd]
        download_exit = utils.pipeline.multi_commands(download_cmd, 2, logger)
        download_end_time = time.time()
        download_time = download_end_time - cwl_start
        if any(x != 0 for x in download_exit):
            cwl_elapsed = download_time
            datetime_end = str(datetime.datetime.now())
            engine = postgres.utils.get_db_engine(postgres_config)
            download_exit_code = next((x for x in download_exit if x != 0), None)
            postgres.utils.set_download_error(download_exit_code, logger, engine,
                                              args.case_id, args.tumor_gdc_id, args.normal_gdc_id, output_id,
                                              datetime_start, datetime_end,
                                              hostname, cwl_version, docker_version,
                                              download_time, cwl_elapsed, statusclass, metricsclass)
            # Exit
            sys.exit(download_exit_code)
        else:
            logger.info("Download successfully. Normal bam is %s, and tumor bam is %s." % (normal_bam, tumor_bam))
        # Build index
        normal_bam_index_cmd = " ".join(['samtools', 'index', normal_bam])
        tumor_bam_index_cmd = " ".join(['samtools', 'index', tumor_bam])
        index_cmd = [normal_bam_index_cmd, tumor_bam_index_cmd]
        index_exit = utils.pipeline.multi_commands(index_cmd, 2, logger)
        if any(x != 0 for x in index_exit):
            logger.info("Failed to build bam index.")
            index_exit_code = next((x for x in index_exit if x != 0), None)
            sys.exit(index_exit_code)
        else:
            normal_bam_index = utils.pipeline.get_index(logger, inputdir, normal_bam)
            tumor_bam_index = utils.pipeline.get_index(logger, inputdir, tumor_bam)
        # Create input json
        input_json_list = []
        for i, block in enumerate(utils.pipeline.fai_chunk(reference_fasta_fai, args.block)):
            input_json_file = os.path.join(jsondir, '{0}.{4}.{1}.{2}.{3}.mutect2.calling.inputs.json'.format(str(output_id), block[0], block[1], block[2], i))
            input_json_data = {
            "java_heap": args.java_heap,
            "ref": {"class": "File", "path": reference_fasta_path},
            "region": "{0}:{1}-{2}".format(block[0], block[1], block[2]),
            "tumor_bam": {"class": "File", "path": tumor_bam},
            "normal_bam": {"class": "File", "path": normal_bam},
            "pon": {"class": "File", "path": pon_path},
            "cosmic": {"class": "File", "path": cosmic_path},
            "dbsnp": {"class": "File", "path": known_snp_vcf_path},
            "cont": contEst,
            "output_name": '{}_{}_{}.calling.vcf.gz'.format(block[0], block[1], block[2]),
            "duscb": duscb
            }
            with open(input_json_file, 'wt') as o:
                json.dump(input_json_data, o, indent=4)
            input_json_list.append(input_json_file)
        logger.info("Preparing input json")
    elif args.pipeline == 'tumor_only':
        # Download input
        tumor_bam = os.path.join(inputdir, os.path.basename(args.tumor_s3_url))
        tumor_download_cmd = utils.s3.aws_s3_get(logger, args.tumor_s3_url, inputdir,
                                                 args.t_s3_profile, args.t_s3_endpoint, recursive=False))
        download_exit = utils.pipeline.run_command(tumor_download_cmd,logger)
        download_end_time = time.time()
        download_time = download_end_time - cwl_start
        if download_exit != 0:
            cwl_elapsed = download_time
            datetime_end = str(datetime.datetime.now())
            engine = postgres.utils.get_db_engine(postgres_config)
            postgres.utils.set_download_error(download_exit, logger, engine,
                                              args.case_id, args.tumor_gdc_id, args.normal_gdc_id, output_id,
                                              datetime_start, datetime_end,
                                              hostname, cwl_version, docker_version,
                                              download_time, cwl_elapsed, statusclass, metricsclass)
            # Exit
            sys.exit(download_exit_code)
        else:
            logger.info("Download successfully. Normal bam is %s, and tumor bam is %s." % (normal_bam, tumor_bam))
        # Build index
        tumor_bam_index_cmd = ['samtools', 'index', tumor_bam]
        index_exit = utils.pipeline.run_command(tumor_bam_index_cmd, logger)
        if index_exit != 0:
            logger.info("Failed to build bam index.")
            sys.exit(index_exit)
        else:
            tumor_bam_index = utils.pipeline.get_index(logger, inputdir, tumor_bam)
        # Create input json
        input_json_list = []
        for i, block in enumerate(utils.pipeline.fai_chunk(reference_fasta_fai, args.block)):
            input_json_file = os.path.join(jsondir, '{0}.{4}.{1}.{2}.{3}.mutect2.calling.inputs.json'.format(str(output_id), block[0], block[1], block[2], i))
            input_json_data = {
            "java_heap": args.java_heap,
            "ref": {"class": "File", "path": reference_fasta_path},
            "region": "{0}:{1}-{2}".format(block[0], block[1], block[2]),
            "tumor_bam": {"class": "File", "path": tumor_bam},
            "pon": {"class": "File", "path": pon_path},
            "cosmic": {"class": "File", "path": cosmic_path},
            "dbsnp": {"class": "File", "path": known_snp_vcf_path},
            "cont": contEst,
            "output_name": '{}_{}_{}.calling.vcf.gz'.format(block[0], block[1], block[2]),
            "duscb": duscb
            }
            with open(input_json_file, 'wt') as o:
                json.dump(input_json_data, o, indent=4)
            input_json_list.append(input_json_file)
        logger.info("Preparing input json")
    else:
        raise Exception("Cannot get correct pipeline: {}. Please choose between 'mutect2' or 'pon' or 'tumor_only'.".format(args.pipeline))
    # Run CWL
    os.chdir(workdir)
    logger.info('Running CWL workflow')
    cmds = list(utils.pipeline.cmd_template(inputdir = inputdir, workdir = workdir, cwl_path = args.cwl, input_json = input_json_list))
    cwl_exit = utils.pipeline.multi_commands(cmds, args.thread_count, logger)
    tmp_vcf_list = glob.glob(os.path.join(workdir, '*.vcf.gz'))
    # Create sort json
    sort_json = utils.pipeline.create_sort_json(reference_fasta_dict, str(output_id), args.pipeline, jsondir, workdir, tmp_vcf_list, logger)
    # Run Sort
    sort_cmd = ['/home/ubuntu/.virtualenvs/p2/bin/cwltool',
               "--debug",
               "--tmpdir-prefix", inputdir,
               "--tmp-outdir-prefix", workdir,
               args.sort,
               sort_json]
    sort_exit = utils.pipeline.run_command(sort_cmd, logger)
    cwl_exit.append(sort_exit)
    # Compress the outputs and CWL logs
    os.chdir(jobdir)
    output_tar = os.path.join(resultdir, "%s.%s.tar.bz2" % (args.pipeline, str(output_id)))
    logger.info("Compressing workflow outputs: %s" % (output_tar))
    utils.pipeline.targz_compress(logger, output_tar, os.path.basename(workdir), cmd_prefix=['tar', '-cjvf'])
    output_vcf_path = os.path.join(resultdir, output_vcf)
    os.rename(os.path.join(workdir, output_vcf), output_vcf_path)
    os.rename(os.path.join(workdir, output_vcf + ".tbi"), os.path.join(resultdir, output_vcf + ".tbi"))
    upload_dir_location = os.path.join(args.s3dir, str(output_id))
    upload_file_location = os.path.join(upload_dir_location, output_vcf)
    # Get md5 and file size
    md5 = utils.pipeline.get_md5(output_vcf_path)
    file_size = utils.pipeline.get_file_size(output_vcf_path)
    # Upload output
    upload_start = time.time()
    logger.info("Uploading workflow output to %s" % (upload_file_location))
    upload_exit  = utils.s3.aws_s3_put(logger, upload_dir_location, resultdir, args.s3_profile, args.s3_endpoint, recursive=True)
    # Establish connection with database
    engine = postgres.utils.get_db_engine(postgres_config)
    # End time
    cwl_end = time.time()
    upload_time = cwl_end - upload_start
    cwl_elapsed = cwl_end - cwl_start
    datetime_end = str(datetime.datetime.now())
    logger.info("datetime_end: %s" % (datetime_end))
    # Get status info
    logger.info("Get status/metrics info")
    status, loc = postgres.status.get_status(upload_exit, cwl_exit, upload_file_location, upload_dir_location, logger)
    # Get metrics info
    time_metrics = utils.pipeline.get_time_metrics(log_file)
    # Set status table
    logger.info("Updating status")
    postgres.utils.add_pipeline_status(engine, args.case_id, args.tumor_gdc_id, args.normal_gdc_id, output_id,
                                       status, loc, datetime_start, datetime_end,
                                       md5, file_size, hostname, cwl_version, docker_version, statusclass)
    # Set metrics table
    logger.info("Updating metrics")
    postgres.utils.add_pipeline_metrics(engine, args.case_id, args.tumor_gdc_id, args.normal_gdc_id, download_time,
                                        upload_time, str(args.thread_count), cwl_elapsed,
                                        sum(time_metrics['system_time'])/float(len(time_metrics['system_time'])),
                                        sum(time_metrics['user_time'])/float(len(time_metrics['user_time'])),
                                        sum(time_metrics['wall_clock'])/float(len(time_metrics['wall_clock'])),
                                        sum(time_metrics['percent_of_cpu'])/float(len(time_metrics['percent_of_cpu'])),
                                        sum(time_metrics['maximum_resident_set_size'])/float(len(time_metrics['maximum_resident_set_size'])),
                                        status, metricsclass)
    # Remove job directories, upload final log file
    logger.info("Uploading main log file")
    utils.s3.aws_s3_put(logger, upload_dir_location + '/' + os.path.basename(log_file), log_file, args.s3_profile, args.s3_endpoint, recursive=False)
    utils.pipeline.remove_dir(jobdir)

if __name__ == '__main__':
    # Get args
    args = get_args()
    pipeline = args.pipeline.lower()
    # Setup postgres classes
    class MuTect2Status(postgres.mixins.StatusTypeMixin, postgres.utils.Base):
        __tablename__ = 'gatk3_6_mutect2_' + pipeline + '_cwl_status'
    class MuTect2Metrics(postgres.mixins.MetricsTypeMixin, postgres.utils.Base):
        __tablename__ = 'gatk3_6_mutect2_' + pipeline + '_cwl_metrics'
    # Run pipeline
    run_pipeline(args, MuTect2Status, MuTect2Metrics)
