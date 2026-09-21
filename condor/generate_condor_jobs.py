#!/usr/bin/env python3
import os
import sys
import argparse

def main():
    parser = argparse.ArgumentParser(description="Generate job_list.txt and condor.jdl for HTCondor submission.")
    parser.add_argument("--file-list", "-f", required=True, help="Input text file containing ROOT file list")
    parser.add_argument("--output-dir", "-o", required=True, help="Output directory path (NFS or XRootD SE path)")
    parser.add_argument("--tag", "-t", default="default", help="Tag name for logs and identification (e.g., dtG0)")
    parser.add_argument("--module", "-m", default="modules.topCPVCategorizer:MODULES", help="PostProc module import specification")
    parser.add_argument("--branch-file", "-b", default="branches/branch_CPV_Run2_MC.txt", help="Branch selection text file")
    parser.add_argument("--test", action="store_true", help="Generate submission files for a single test job only")

    args = parser.parse_args()

    file_list_path = os.path.abspath(args.file_list)
    output_dir = args.output_dir
    tag = args.tag
    module_import = args.module
    branch_file = args.branch_file

    current_dir = os.path.dirname(os.path.abspath(__file__)) # NtupleForge/condor/
    tarball_path = os.path.join(current_dir, "CMSSW_14_2_1_full.tar.gz")
    wrapper_path = os.path.join(current_dir, "run_condor.sh")
    log_dir = os.path.join(current_dir, "condor_logs", tag)

    print("=== [Condor Job Generator] ===")
    print(f"Input List File: {file_list_path}")
    print(f"Output Directory: {output_dir}")
    print(f"Tag Name        : {tag}")
    print(f"Module Spec     : {module_import}")
    print(f"Branch Spec     : {branch_file}")
    print(f"Test Mode       : {args.test}")

    if not os.path.exists(file_list_path):
        print(f"[ERROR] Input file list does not exist: {file_list_path}")
        sys.exit(1)

    if not os.path.exists(tarball_path):
        print(f"[ERROR] Tarball does not exist: {tarball_path}")
        print("Please run 'python3 condor/make_tarball.py' first.")
        sys.exit(1)

    # VOMS Proxy path detection
    uid = os.getuid()
    proxy_path = f"/tmp/x509up_u{uid}"
    use_proxy_str = ""
    if os.path.exists(proxy_path):
        use_proxy_str = f"""use_x509userproxy     = True
x509userproxy         = {proxy_path}"""
        print(f"[INFO] VOMS Proxy detected and enabled: {proxy_path}")
    else:
        print("[WARNING] No VOMS Proxy found in /tmp. If XRootD auth fails, run 'voms-proxy-init --voms cms'.")

    # Ensure log directory exists
    os.makedirs(log_dir, exist_ok=True)

    # Read input file list
    with open(file_list_path, "r") as f:
        input_files = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not input_files:
        print(f"[ERROR] No valid files found in {file_list_path}")
        sys.exit(1)

    if args.test:
        input_files = input_files[:1]
        print("[INFO] Test mode active: Processing only 1 file.")

    # Write job_list.txt
    job_list_path = os.path.join(current_dir, "job_list.txt")
    with open(job_list_path, "w") as f:
        for infile in input_files:
            f.write(f"{infile} {output_dir} {module_import} {branch_file}\n")

    print(f"[SUCCESS] Generated job list with {len(input_files)} jobs: {job_list_path}")

    # Write condor.jdl
    jdl_path = os.path.join(current_dir, "condor.jdl")
    jdl_content = f"""universe              = vanilla
executable            = {wrapper_path}
arguments             = $(input_file) $(output_dir) $(module_import) $(branch_file)

output                = {log_dir}/job_$(Cluster)_$(Process).out
error                 = {log_dir}/job_$(Cluster)_$(Process).err
log                   = {log_dir}/job_$(Cluster).log

{use_proxy_str}

transfer_input_files  = {tarball_path}

should_transfer_files = YES
when_to_transfer_output = ON_EXIT

request_cpus          = 1
request_memory        = 2000MB
request_disk          = 5GB

queue input_file, output_dir, module_import, branch_file from {job_list_path}
"""

    with open(jdl_path, "w") as f:
        f.write(jdl_content)

    print(f"[SUCCESS] Generated Condor JDL: {jdl_path}")
    print("\n[INFO] Ready to submit! Run:")
    print(f"   condor_submit {jdl_path}")

if __name__ == "__main__":
    main()
