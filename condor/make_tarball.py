#!/usr/bin/env python3
import os
import sys
import subprocess
import argparse

def main():
    parser = argparse.ArgumentParser(description="Script to compress the entire CMSSW_14_2_1 directory into a tarball")
    parser.add_argument("--force", "-f", action="store_true", help="Force re-compression even if tarball already exists")
    args = parser.parse_args()

    # Automatically determine directories relative to this script
    current_dir = os.path.dirname(os.path.abspath(__file__)) # NtupleForge/condor/
    ntupleforge_dir = os.path.abspath(os.path.join(current_dir, "..")) # NtupleForge/
    src_dir = os.path.abspath(os.path.join(ntupleforge_dir, "..")) # CMSSW_14_2_1/src/
    cmssw_base_dir = os.path.abspath(os.path.join(src_dir, "..")) # CMSSW_14_2_1/
    cmssw_parent_dir = os.path.abspath(os.path.join(cmssw_base_dir, "..")) # Parent directory of CMSSW

    cmssw_name = os.path.basename(cmssw_base_dir) # CMSSW_14_2_1
    tarball_name = f"{cmssw_name}_full.tar.gz"
    tarball_path = os.path.join(current_dir, tarball_name)

    print("=== [CMSSW Tarball Builder] ===")
    print(f"Target CMSSW Path : {cmssw_base_dir}")
    print(f"Output Tarball     : {tarball_path}")

    if os.path.exists(tarball_path) and not args.force:
        size_mb = os.path.getsize(tarball_path) / (1024 * 1024)
        print(f"[WARNING] Tarball already exists: {tarball_name} ({size_mb:.1f} MB)")
        print("Use --force option to regenerate.")
        return

    print("\n[INFO] Compressing tarball... (This may take a few minutes)")

    # Build tar command with exclusion of tarball itself
    tar_cmd = [
        "tar",
        "--exclude=.git",
        "--exclude=tmp",
        "--exclude=ib-rectify*",
        "--exclude=*.pyc",
        "--exclude=*.tar.gz",
        "-czf",
        tarball_path,
        "-C", cmssw_parent_dir,
        cmssw_name
    ]

    try:
        subprocess.run(tar_cmd, check=True)
        size_mb = os.path.getsize(tarball_path) / (1024 * 1024)
        print(f"[SUCCESS] Compression complete! Created: {tarball_path} ({size_mb:.1f} MB)")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Error occurred during tarball compression: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
