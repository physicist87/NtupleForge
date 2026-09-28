#!/usr/bin/env python3
"""
NtupleForge CRAB Manager
=============================
[Description]
Reads a YAML configuration file and manages CRAB jobs (Submit, Status, Report,
Resubmit, Kill). Uses 'crab/crab_script.py' as the worker node wrapper.

[Usage]
python3 crab/submit_crab.py --config crabConfig/config_crabTest.yaml             # submit (auto-resubmits existing tasks)
python3 crab/submit_crab.py --config crabConfig/config_crabTest.yaml --status    # full crab status per task
python3 crab/submit_crab.py --config crabConfig/config_crabTest.yaml --report    # compact per-sample job-state summary
python3 crab/submit_crab.py --config crabConfig/config_crabTest.yaml --resubmit  # explicit resubmit of failed jobs
python3 crab/submit_crab.py --config crabConfig/config_crabTest.yaml --kill      # kill all tasks
"""

import os
import sys
import glob
import argparse
import yaml
import shutil
import logging
import subprocess
import io
import contextlib
import json
import urllib.request
from CRABClient.UserUtilities import config, getUsername
from CRABAPI.RawCommand import crabCommand

try:
    from http.client import HTTPException
except ImportError:
    from httplib import HTTPException

# Logging Setup
logging.basicConfig(level=logging.INFO, format='[submit_crab] : %(message)s')
logger = logging.getLogger("Submitter")

# --- Job-state buckets for --report -----------------------------------------
# CRAB job states shown as their own column in the compact report.
REPORT_COLUMNS = ["finished", "running", "idle", "transferring", "failed"]
# Known-but-minor states folded into "others" WITHOUT raising an unknown warning.
KNOWN_OTHER_STATES = {
    "unsubmitted", "cooloff", "held", "killed", "killing",
    "toRetry", "on hold", "resubmitting",
}

def summarize_status(jobs_per_status):
    """Bucket a CRAB ``jobsPerStatus`` dict into the report columns + 'others'.

    Returns ``(row, unknown)`` where ``row`` maps each REPORT_COLUMNS entry plus
    ``others`` and ``total`` to a count, and ``unknown`` is the set of state
    names that are neither a column nor a known-other state -- i.e. states the
    code does not recognise, so the caller can warn about them.
    """
    row = {c: 0 for c in REPORT_COLUMNS}
    row["others"] = 0
    unknown = set()
    for state, n in (jobs_per_status or {}).items():
        if state in REPORT_COLUMNS:
            row[state] += n
        else:
            row["others"] += n
            if state not in KNOWN_OTHER_STATES:
                unknown.add(state)
    row["total"] = sum(row[c] for c in REPORT_COLUMNS) + row["others"]
    return row, unknown

def print_report(rows):
    """Print a compact per-sample job-state table. ``rows``: list of (name, row)."""
    cols = REPORT_COLUMNS + ["others", "total"]
    head = {"finished": "done", "running": "run", "idle": "idle",
            "transferring": "transf", "failed": "fail", "others": "other",
            "total": "total"}
    name_w = max([len("sample")] + [len(n) for n, _ in rows])
    header = f"{'sample':<{name_w}}  " + "  ".join(f"{head[c]:>6}" for c in cols)
    bar = "=" * len(header)
    print("\n" + bar)
    print("CRAB job report (per sample)  [done=finished, transf=transferring]")
    print(bar)
    print(header)
    print("-" * len(header))
    agg = {c: 0 for c in cols}
    for name, row in rows:
        print(f"{name:<{name_w}}  " + "  ".join(f"{row[c]:>6}" for c in cols))
        for c in cols:
            agg[c] += row[c]
    print("-" * len(header))
    print(f"{'TOTAL':<{name_w}}  " + "  ".join(f"{agg[c]:>6}" for c in cols))
    print(bar)

def check_voms():
    """Checks if VOMS proxy is valid."""
    try:
        subprocess.run(["voms-proxy-info", "--exists"], check=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        logger.error("VOMS Proxy missing or expired.")
        logger.error("Run: voms-proxy-init --voms cms --valid 168:00")
        sys.exit(1)

def main(args):
    check_voms()

    # 1. Load Configuration
    if not os.path.exists(args.config):
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)

    with open(args.config, 'r') as f:
        try:
            cfg = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"YAML Error: {e}")
            sys.exit(1)

    common = cfg.get('common', {})
    datasets = cfg.get('datasets', {})
    
    logger.info(f"Loaded Configuration: {args.config}")
    logger.info(f"Common Work Area: {common.get('jobID', 'crab_projects')}")
    logger.info(f"Target Datasets: {len(datasets)}")
    logger.info("="*60)

    # 2. Setup CRAB Configuration
    conf = config()
    
    # -- General --
    conf.General.transferOutputs = True
    conf.General.transferLogs = True
    conf.General.workArea = common.get('jobID', 'crab_projects')
    
    # -- JobType --
    conf.JobType.pluginName = 'Analysis'
    conf.JobType.psetName = 'crab/PSet.py'
    conf.JobType.scriptExe = 'crab/crab_script.py' 
    conf.JobType.maxMemoryMB = common.get('max_memory', 2500)
    
    splitting_mode = common.get('splitting', 'Automatic')
    if splitting_mode != 'Automatic':
        conf.JobType.maxJobRuntimeMin = common.get('max_runtime', 600)


    # ------------------------------------------------------
    # Input Filename Logic
    # ------------------------------------------------------
    # Files to ship to worker node
    # CRAB will flatten directory structure, placing it in root dir on worker.
    conf.JobType.inputFiles = ['script/run_postproc.py'] # Main Script

    # ------------------------------------------------------
    # Module Handling (List-based YAML)
    # ------------------------------------------------------
    # YAML Format: analysis_module: ["modules/jetsMETcut.py", "MODULES"]
    
    module_cfg = common.get('analysis_module') # Returns a list: [path, list_name]
    worker_module_arg = None # Will store string "jetsMETcut:MODULES" for worker

    if module_cfg and len(module_cfg) == 2:
        local_path = module_cfg[0]  # e.g., "modules/jetsMETcut.py"
        list_var   = module_cfg[1]  # e.g., "MODULES"

        if os.path.exists(local_path):
            conf.JobType.inputFiles.append(local_path)
            logger.info(f"Adding Module File: {local_path}")

            # ------------------------------------------------------
            # Auto-include helper modules from the same directory.
            #
            # CRAB flattens the sandbox into the worker's cwd, so any
            # helper module the analysis module imports must be shipped
            # explicitly. We ship EVERY sibling ".py" in the module
            # directory (except the analysis module itself and dunders).
            #
            # NOTE: previously only files matching "_*.py" were auto-
            # included, which coupled a helper's *name* to whether it
            # shipped — renaming a helper without a leading underscore
            # silently dropped it from the sandbox and broke the job at
            # import time. Naming is now decoupled from shipping.
            #
            # The analysis module must still resolve the helper import
            # in a flat/top-level context (CRAB imports it flat): put
            # its own directory on sys.path via __file__, then import.
            # ------------------------------------------------------
            module_dir = os.path.dirname(local_path) or "."
            analysis_basename = os.path.basename(local_path)
            helper_files = sorted(
                h for h in glob.glob(os.path.join(module_dir, "*.py"))
                if os.path.basename(h) != analysis_basename
                and not os.path.basename(h).startswith("__")
            )
            for h in helper_files:
                conf.JobType.inputFiles.append(h)
                logger.info(f"  -> Auto-included helper: {h}")

            # Prepare Argument for Worker Node
            # Worker sees flat files. "modules/jetsMETcut.py" -> "jetsMETcut.py"
            # Argument format: "jetsMETcut:MODULES" (drop extension, append list var)
            file_basename = os.path.basename(local_path) # jetsMETcut.py
            module_name_only = os.path.splitext(file_basename)[0] # jetsMETcut
            worker_module_arg = f"{module_name_only}:{list_var}"
            
        else:
            logger.error(f"CRITICAL: Module file not found at {local_path}")
            sys.exit(1)
    elif module_cfg:
        logger.error(f"YAML Error: 'analysis_module' must be a list with 2 elements [path, list_name]. Got: {module_cfg}")
        sys.exit(1)

    # ------------------------------------------------------
    # Branch File Handling
    # ------------------------------------------------------
    branch_sel = common.get('branch_file') # Renamed from branch_path
    
    if branch_sel:
        if os.path.exists(branch_sel):
            conf.JobType.inputFiles.append(branch_sel)
            logger.info(f"Adding Branch File: {branch_sel}")
        else:
            logger.error(f"CRITICAL: Branch file not found at {branch_sel}")
            sys.exit(1)
            
    # Add YAML Config (Provenance)
    conf.JobType.inputFiles.append(args.config)

    # ------------------------------------------------------
    # Golden JSON Handling (Data only)
    # ------------------------------------------------------
    # The Golden JSON URL is specified in the YAML configuration.
    # Download it on the submit host and ship the local copy in the
    # CRAB sandbox. The worker passes it to NanoAODTools via -J.
    golden_json_file = None
    sample_type = str(common.get('sample_type', '')).strip().lower()
    golden_json_url = common.get('golden_json')

    if sample_type == 'data':
        if not golden_json_url:
            logger.error("CRITICAL: Data configuration requires 'golden_json'")
            sys.exit(1)

        golden_json_file = os.path.basename(golden_json_url)

        try:
            logger.info(f"Downloading Golden JSON: {golden_json_url}")
            urllib.request.urlretrieve(golden_json_url, golden_json_file)

            # Validate that the downloaded file is valid JSON.
            with open(golden_json_file, 'r') as jf:
                golden_data = json.load(jf)

            if not isinstance(golden_data, dict) or not golden_data:
                raise ValueError("Golden JSON must contain a non-empty JSON object")

            conf.JobType.inputFiles.append(golden_json_file)
            logger.info(f"Adding Golden JSON to CRAB sandbox: {golden_json_file}")

        except Exception as e:
            if golden_json_file and os.path.exists(golden_json_file):
                os.remove(golden_json_file)
            logger.error(f"CRITICAL: Failed to prepare Golden JSON: {e}")
            sys.exit(1)

    elif golden_json_url:
        logger.warning(
            "golden_json is defined for a non-Data configuration; ignoring it"
        )


    # ------------------------------------------------------
    # Output Filename Logic
    # ------------------------------------------------------
    # Default output filename (should match process.output.fileName in the PSet)
    out_name = "forgedNtuple.root"


    # -- Arguments File Generation --
    args_file = "crab_args.txt"
    with open(args_file, "w") as f:
        # Branch Arg
        if branch_sel: 
            f.write(f"-b\n{os.path.basename(branch_sel)}\n")
        
        # Module Arg (Optimized)
        if worker_module_arg: 
            f.write(f"-I\n{worker_module_arg}\n")
            
        # Golden JSON Arg (Data only)
        if golden_json_file:
            f.write(f"-J\n{os.path.basename(golden_json_file)}\n")

        if common.get('max_events'): 
            f.write(f"-N\n{common.get('max_events')}\n")

        # Pass the output filename to the worker node script
        f.write(f"--output-file={out_name}\n")

    conf.JobType.inputFiles.append(args_file)
    conf.JobType.scriptArgs = [] 

    # ------------------------------------------------------
    # Output Files Configuration (Provenance)
    # ------------------------------------------------------
    # Instruct CRAB to transfer these files back to the output storage.
    # 1. out_name: Main output file
    # 2. crab_args.txt: List of arguments used for the job
    # 3. YAML Config: The configuration file used for submission
    conf.JobType.outputFiles = [
        out_name,            
        ##'crab_args.txt',
        ##os.path.basename(args.config)
    ]

    # -- Data & Site --
    conf.Data.inputDBS = 'global'
    
    # [FIX] Splitting Logic (Automatic vs FileBased)
    conf.Data.splitting = splitting_mode
    
    # units_per_job means different things:
    # Automatic -> Minutes (e.g., 180)
    # FileBased -> Number of Files (e.g., 1)
    user_units = common.get('units_per_job', 1)
    ##user_units = common.get('units_per_job', 180) # Default 180 mins
    
    conf.Data.unitsPerJob = user_units
    conf.Data.publication = False

    username = getUsername()
    base_out = common.get('output_base', '')
    if base_out:
         conf.Data.outLFNDirBase = f'/store/user/{username}/{base_out.lstrip("/")}'
    else:
         conf.Data.outLFNDirBase = f'/store/user/{username}/'

    conf.Site.storageSite = common.get('site', 'T3_KR_KNU')

    # Accumulators for --report (printed once, after the loop, so columns align)
    report_rows = []
    report_unknown = set()

    # 3. Process Jobs
    for short_name, dataset in datasets.items():
        req_name = short_name.replace("-", "_")

        conf.General.requestName = req_name
        conf.Data.inputDataset = dataset
        conf.Data.outputDatasetTag = short_name

        project_dir = os.path.join(conf.General.workArea, "crab_" + req_name)

        print(f"[{short_name}] Processing...")

        # -- STATUS Action --
        if args.status:
            if os.path.isdir(project_dir):
                try:
                    subprocess.run(["crab", "status", "-d", project_dir], check=True)
                except: pass
            else:
                logger.warning("Project not found.")
            continue

        # -- REPORT Action (compact per-sample job-state summary) --
        if args.report:
            if os.path.isdir(project_dir):
                try:
                    # Silence CRAB's verbose status dump; we only want the dict.
                    with contextlib.redirect_stdout(io.StringIO()):
                        res = crabCommand('status', dir=project_dir)
                    row, unknown = summarize_status(res.get('jobsPerStatus', {}))
                    report_rows.append((short_name, row))
                    report_unknown |= unknown
                except Exception as e:
                    logger.error(f"Status query failed for {short_name}: {e}")
                    report_rows.append((short_name, summarize_status({})[0]))
            else:
                logger.warning("Project not found.")
            continue

        # -- RESUBMIT Action (explicit; failed jobs only, default resources) --
        if args.resubmit:
            if os.path.isdir(project_dir):
                logger.info("Resubmitting (explicit)...")
                try:
                    crabCommand('resubmit', dir=project_dir)
                except Exception as e:
                    logger.error(f"Resubmit Failed: {e}")
            else:
                logger.warning("Project not found (nothing to resubmit).")
            continue

        # -- SUBMIT / RESUBMIT Logic (default action) --
        if os.path.isdir(project_dir):
            logger.info("Resubmitting...")
            try:
                crabCommand('resubmit', dir=project_dir)
            except Exception as e:
                logger.error(f"Resubmit Failed: {e}")
        else:
            logger.info("Submitting...")
            try:
                crabCommand('submit', config=conf)
            except Exception as e:
                logger.error(f"Submit Failed: {e}")

        # -- KILL Action --
        if args.kill:
            if os.path.isdir(project_dir):
                logger.info("Action: KILLING Task")
                try:
                    crabCommand('kill', dir=project_dir)
                    logger.info("Kill command sent successfully.")
                except HTTPException as hte:
                    logger.error(f"Kill Failed: {hte.headers}")
                except Exception as e:
                    logger.error(f"Kill Failed: {e}")
            else:
                logger.warning(f"Project directory not found (nothing to kill): {project_dir}")
            print("-" * 60)
            continue

    # -- Post-loop: print the compact report (if requested) --
    if args.report:
        if report_rows:
            print_report(report_rows)
        if report_unknown:
            logger.warning(
                "Unknown CRAB job state(s) counted under 'others': "
                f"{sorted(report_unknown)}. The report code does not recognise "
                "these -- add them to REPORT_COLUMNS / KNOWN_OTHER_STATES in "
                "crab/submit_crab.py (see summarize_status()), and inspect the "
                "full `crab status -d <project_dir>` output for what they mean."
            )

    # -- Post-loop: remind about memory/walltime resubmits (submit & resubmit only) --
    if not (args.status or args.report or args.kill):
        logger.info("-" * 60)
        logger.info("NOTE: (re)submit here uses DEFAULT resources. Jobs that failed on "
                    "memory or walltime will fail again on a plain resubmit.")
        logger.info("      Resubmit those by hand in the CRAB project dir with raised limits, e.g.:")
        logger.info("        crab resubmit -d <workArea>/crab_<reqName> --maxmemory=4000 --maxjobruntime=2700")
        logger.info("      See docs/troubleshooting.md (CRAB resubmit) for exit codes and details.")

    # Cleanup temporary submission files
    if os.path.exists(args_file):
        os.remove(args_file)

    if golden_json_file and os.path.exists(golden_json_file):
        os.remove(golden_json_file)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YAML based CRAB Manager")
    parser.add_argument("-c", "--config", required=True, help="Path to YAML config")
    parser.add_argument("--status", action="store_true", help="Run full 'crab status' for every task in the config")
    parser.add_argument("--report", action="store_true",
                        help="Compact per-sample job-state summary "
                             "(done/run/idle/transf/fail/other) -- simpler and "
                             "easier to read than full 'crab status'")
    parser.add_argument("--resubmit", action="store_true",
                        help="Explicitly resubmit failed jobs in existing tasks "
                             "(default resources; raise memory/walltime by hand)")
    parser.add_argument("--kill", action="store_true", help="Kill all jobs defined in the config")
    args = parser.parse_args()
    main(args)
