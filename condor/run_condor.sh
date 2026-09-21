#!/usr/bin/env bash
# Worker node execution wrapper for NtupleForge under HTCondor

INPUT_FILE=$1
OUTPUT_DIR=$2
MODULE_IMPORT=$3
BRANCH_FILE=$4

FILENAME=$(basename ${INPUT_FILE})

echo "=================================================="
echo "=== [NtupleForge Condor Worker Job Started] ==="
echo "Host: $(hostname)"
echo "Date: $(date)"
echo "Scratch Dir: $_CONDOR_SCRATCH_DIR"
echo "X509_USER_PROXY: ${X509_USER_PROXY:-'Not Set'}"
echo "Input File : ${INPUT_FILE}"
echo "Output Dir : ${OUTPUT_DIR}"
echo "Module     : ${MODULE_IMPORT}"
echo "Branch File: ${BRANCH_FILE}"
echo "=================================================="

# XRootD and CVMFS Environment Setup
export XRD_REQUESTTIMEOUT=300
export XRD_REDIRECTLIMIT=5
source /cvmfs/cms.cern.ch/cmsset_default.sh
export SCRAM_ARCH=el8_amd64_gcc12

# 1. Unpack CMSSW Tarball
echo -e "\n--> [1/4] Unpacking CMSSW Tarball..."
tar -xzf $_CONDOR_SCRATCH_DIR/CMSSW_14_2_1_full.tar.gz -C $_CONDOR_SCRATCH_DIR/

# 2. Relocate SCRAM Runtime to Worker Local Scratch
echo -e "\n--> [2/4] Setting up SCRAM Environment..."
cd $_CONDOR_SCRATCH_DIR/CMSSW_14_2_1/src
eval `scramv1 runtime -sh`
scram b ProjectRename

# Move to NtupleForge directory
cd NtupleForge

# 3. Run Post-Processor
echo -e "\n--> [3/4] Running Post-Processor..."
python3 script/run_postproc.py ${INPUT_FILE} \
  -I ${MODULE_IMPORT} \
  -b ${BRANCH_FILE} \
  -o $_CONDOR_SCRATCH_DIR/${FILENAME}

POSTPROC_EXIT_CODE=$?
if [ ${POSTPROC_EXIT_CODE} -ne 0 ]; then
    echo "[ERROR] run_postproc.py failed with exit code ${POSTPROC_EXIT_CODE}"
    exit ${POSTPROC_EXIT_CODE}
fi

# 4. Stage-out Result to Target Directory
echo -e "\n--> [4/4] Staging-out output file to target directory..."

if [[ "${OUTPUT_DIR}" == root://* ]]; then
    xrdcp -f $_CONDOR_SCRATCH_DIR/${FILENAME} ${OUTPUT_DIR}/${FILENAME}
    STAGEOUT_EXIT_CODE=$?
else
    mkdir -p ${OUTPUT_DIR}
    cp -f $_CONDOR_SCRATCH_DIR/${FILENAME} ${OUTPUT_DIR}/${FILENAME}
    STAGEOUT_EXIT_CODE=$?
fi

if [ ${STAGEOUT_EXIT_CODE} -ne 0 ]; then
    echo "[ERROR] Stage-out failed with exit code ${STAGEOUT_EXIT_CODE}"
    exit ${STAGEOUT_EXIT_CODE}
fi

# Cleanup local scratch file
rm -f $_CONDOR_SCRATCH_DIR/${FILENAME}

echo -e "\n[SUCCESS] Job Completed Successfully!"
