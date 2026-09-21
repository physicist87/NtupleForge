#!/usr/bin/env python3
import os
import sys
import glob
import argparse

def convert_to_xrootd(path):
    """Converts local /pnfs path to KNU XRootD URL format if applicable."""
    knu_prefix = "/pnfs/knu.ac.kr/data/cms"
    xrootd_prefix = "root://cluster142.knu.ac.kr/"
    
    if path.startswith(knu_prefix):
        rel_path = path[len(knu_prefix):] # Strip /pnfs/knu.ac.kr/data/cms
        return f"{xrootd_prefix}{rel_path}"
    return path

def main():
    parser = argparse.ArgumentParser(description="Scan input directory and generate a text file containing ROOT file list.")
    parser.add_argument("--input-dir", "-i", required=True, help="Input directory path containing ROOT files (NFS or /pnfs)")
    parser.add_argument("--output-txt", "-o", required=True, help="Output text file path (e.g., condor/input_lists/dtG0.txt)")
    parser.add_argument("--pattern", "-p", default="*.root", help="File pattern to search (default: *.root)")
    parser.add_argument("--use-xrootd", action="store_true", help="Automatically convert /pnfs paths to KNU XRootD URLs")

    args = parser.parse_args()

    input_dir = os.path.abspath(args.input_dir)
    output_txt = args.output_txt

    print("=== [Input File List Generator] ===")
    print(f"Scanning Directory : {input_dir}")
    print(f"Search Pattern     : {args.pattern}")

    if not os.path.exists(input_dir):
        print(f"[ERROR] Input directory does not exist: {input_dir}")
        sys.exit(1)

    # Search for files matching pattern
    search_path = os.path.join(input_dir, args.pattern)
    matched_files = sorted(glob.glob(search_path))

    if not matched_files:
        search_path = os.path.join(input_dir, "**", args.pattern)
        matched_files = sorted(glob.glob(search_path, recursive=True))

    if not matched_files:
        print(f"[WARNING] No files matching '{args.pattern}' were found in {input_dir}")
        sys.exit(1)

    # Ensure output directory exists
    out_dir = os.path.dirname(os.path.abspath(output_txt))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    # Write file paths
    with open(output_txt, "w") as f:
        for filepath in matched_files:
            final_path = convert_to_xrootd(filepath) if args.use_xrootd else filepath
            f.write(f"{final_path}\n")

    print(f"[SUCCESS] Found {len(matched_files)} files. Saved to: {output_txt}")

if __name__ == "__main__":
    main()
