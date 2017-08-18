#!/bin/bash
#SBATCH --nodes=1
#SBATCH --cpus-per-task=XX_THREAD_COUNT_XX
#SBATCH --ntasks=1
#SBATCH --mem=XX_MEM_XX
#SBATCH --workdir="/mnt/SCRATCH/"

function cleanup (){
    echo "cleanup tmp data";
    sudo rm -rf $basedir;
}

case_id="XX_CASEID_XX"
tumor_gdc_id="XX_TID_XX"
normal_gdc_id="XX_NID_XX"
normal_filesize="XX_FILESIZE_XX"
tumor_s3_url="XX_TS3URL_XX"
t_s3_profile="XX_TS3PROFILE_XX"
t_s3_endpoint="XX_TS3ENDPOINT_XX"
normal_s3_url="XX_NS3URL_XX"
n_s3_profile="XX_NS3PROFILE_XX"
n_s3_endpoint="XX_NS3ENDPOINT_XX"
pipeline="XX_PIPELINE_XX"

basedir=`sudo mktemp -d XX_PIPELINE_XX.XXXXXXXXXX -p /mnt/SCRATCH/`
refdir="XX_REFDIR_XX"
s3dir="XX_S3DIR_XX"
s3_profile="XX_S3PROFILE_XX"
s3_endpoint="XX_S3ENDPOINT_XX"

block="XX_BLOCKSIZE_XX"
thread_count="XX_THREAD_COUNT_XX"
java_heap="XX_JAVAHEAP_XX"

repository="git@github.com:NCI-GDC/mutect2-cwl.git"
sudo chown ubuntu:ubuntu $basedir

cd $basedir

sudo git clone -b feat/develop $repository mutect2_cwl
sudo chown ubuntu:ubuntu -R mutect2_cwl

trap cleanup EXIT

/home/ubuntu/.virtualenvs/p2/bin/python mutect2_cwl/slurm/gdc_mutect2_pipeline.py \
--case_id $case_id \
--tumor_gdc_id $tumor_gdc_id \
--normal_gdc_id $normal_gdc_id \
--normal_filesize $normal_filesize \
--tumor_s3_url $tumor_s3_url \
--t_s3_profile $t_s3_profile \
--t_s3_endpoint $t_s3_endpoint \
--normal_s3_url $normal_s3_url \
--n_s3_profile $n_s3_profile \
--n_s3_endpoint $n_s3_endpoint \
--pipeline $pipeline \
--basedir $basedir \
--refdir $refdir \
--cwl $basedir/mutect2_cwl/tools/$pipeline/tool.cwl \
--sort $basedir/mutect2_cwl/tools/picard-sortvcf.cwl \
--s3dir $s3dir \
--s3_profile $s3_profile \
--s3_endpoint $s3_endpoint \
--block $block \
--thread_count $thread_count \
--java_heap $java_heap