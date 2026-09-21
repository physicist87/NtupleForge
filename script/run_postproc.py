#!/usr/bin/env python3
"""
NtupleForge: NanoAOD Post-Processing Framework
==============================================

Description:
    This script serves as a lightweight wrapper around the standard CMS NanoAODTools PostProcessor.
    It facilitates event skimming (cuts), branch slimming (keep/drop), and the application of 
    custom analysis modules (Python-based) to NanoAOD Ntuples.

Mechanism:
    1. Parses input arguments (Input files, Modules, Branch selection).
    2. Load specified Python modules dynamically.
    3. Configures the PostProcessor engine with hardcoded defaults for consistency.
    4. Runs the event loop, producing a new ROOT file (Skim).
    5. Optionally merges multiple outputs into a single file if --output-file is specified.

Usage Examples:
    # 1. Basic Split Mode (One output per input)
    python3 script/run_postproc.py input1.root input2.root \
        -b branches/branch_keep_and_drop.txt \
        -I modules.jetsMETcut:MODULES

    # 2. Merge Mode (Hadd multiple inputs into one output)
    python3 script/run_postproc.py input*.root \
        -b branches/branch_keep_and_drop.txt \
        -I modules.jetsMETcut:MODULES \
        --output-file merged_skim.root

    # 3. Local validation run with ttbarCategorizer debug CSV
    python3 script/run_postproc.py input.root \
        -b branches/branch_ttHHto4b_hadronic_2017UL.txt \
        -I modules.ttbarCategorizer:MODULES \
        -N 1000 \
        --ttcat-debug-csv \
        --ttcat-debug-csv-path /tmp/ttcat_check.csv

[Note on YAML Configuration]
When submitting jobs via CRAB using 'submit_crab.py', the arguments for this script 
(such as --imports and --branch-selection) are derived from the YAML config file 
(e.g., 'crabConfig/config_crabTest.yaml'). 
Make sure the values in the YAML file correctly point to existing files and modules.

[Branch Selection Policy — INPUT vs OUTPUT]
The keep/drop file passed via -b is applied ONLY to the OUTPUT tree
(`outputbranchsel`). The INPUT tree is NOT filtered (`branchsel=None`),
so modules can freely read any branch present in the source NanoAOD,
including gen-level branches (GenPart_*, GenJet_*, genTtbarId) needed
for ttbar categorization.

Why this matters:
    Setting `branchsel=args.branch_selection` (the previous behaviour)
    applied `drop *` to BOTH input and output. The driver then re-
    enabled only the listed `keep` branches on the input tree, but
    the way nanoAOD-tools normalizes wildcard rules vs explicit names
    sometimes left vector branches in a "hasattr=True / len()=0"
    zombie state. Result: 1000/1000 events fell into the NOGEN path
    of ttbarCategorizer despite GenPart being listed in keep rules.
    See debugging session 2026-04-06.

Author: Junghyun Lee (NtupleForge)
"""

import os
import sys
import argparse
import importlib
import logging
import datetime
from PhysicsTools.NanoAODTools.postprocessing.framework.postprocessor import PostProcessor


# -------------------------------------------------------------------------
# Path Configuration (For ModuleNotFoundError)
# -------------------------------------------------------------------------
# Add the parent directory (NtupleForge root) to sys.path so that 'modules' package can be found
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)


# -------------------------------------------------------------------------
# Logging Setup
# -------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='[run_postproc] : %(message)s')
logger = logging.getLogger("NtupleForge")

def main():
    # Print execution timestamp immediately
    start_time = datetime.datetime.now()
    logger.info("="*60)
    logger.info(f"Execution Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*60)

    parser = argparse.ArgumentParser(description="NtupleForge: Minimal Post-Processing Script")

    # [1] Essential Arguments
    # Input files: Accepts a list of ROOT file paths directly (relies on shell expansion)
    parser.add_argument("input_files", type=str, nargs="+", help="Input ROOT file paths (space separated)")
    
    # Module & Branch Configuration
    parser.add_argument("-I", "--imports", type=str, nargs="+", required=True,
                        help="Modules to import (Format: 'module_name:list_name', e.g., modules.jetsMETcut:MODULES)")
    parser.add_argument("-b", "--branch-selection", type=str, required=True,
                        help="Path to branch selection file (keep/drop rules for OUTPUT tree only)")

    # [2] Optional Output Configuration
    # If provided, triggers 'hadd' to merge all outputs into this filename.
    # If not provided, defaults to split mode (one output file per input file).
    parser.add_argument("-o", "--output-file", type=str, default=None, 
                        help="Merge all outputs into this filename (e.g., skimmed.root)")

    # Event Control (Default: None = Process All Events)
    parser.add_argument("-N", "--max-events", type=int, default=None, 
                        help="Max number of events to process. Default is None (Run All).")
    parser.add_argument("--first-entry", type=int, default=0,
                        help="Index of the first event to process. Default is 0.")
    parser.add_argument(
        "--json-input", type=str, default=None,
        help="JSON run/lumi selection for PostProcessor. In CRAB Data jobs this "
             "is generated automatically from the lumis assigned after the "
             "official Golden JSON lumiMask is applied."
    )

    # [3] ttbarCategorizer Options
    # ────────────────────────────────────────────────────────────────────
    # These flags control the optional debug behaviour of the
    # TtbarCategorizer module (modules/ttbarCategorizer.py). They are
    # passed to the module via environment variables, which the
    # `make_default_module()` factory reads when constructing the
    # MODULES list at import time.
    #
    # The categorizer ALWAYS writes both branch sets (`ttCat_*` and
    # `ttCatXval_*`) regardless of these flags. The flags only control
    # the optional CSV dump and the endJob stderr report.
    # ────────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--ttcat-debug-csv",
        action="store_true",
        help="Enable per-event ttbarCategorizer debug CSV. The CSV "
             "duplicates information already in the ntuple branches; "
             "use only for interactive local validation. NOT staged out "
             "by CRAB — do not enable for production jobs."
    )
    parser.add_argument(
        "--ttcat-debug-csv-path",
        type=str,
        default=None,
        help="Output path for the ttcat debug CSV (only effective with "
             "--ttcat-debug-csv). Default: ./ttcat_debug.csv"
    )
    parser.add_argument(
        "--ttcat-quiet",
        action="store_true",
        help="Suppress the ttbarCategorizer endJob stderr report "
             "(source distribution + category counts + confusion matrix)."
    )

    args = parser.parse_args()

    # ────────────────────────────────────────────────────────────────────
    # Pass ttcat options through to the module via environment variables.
    # This is set BEFORE module imports happen below, so make_default_module()
    # sees the updated environment when it constructs the categorizer.
    # ────────────────────────────────────────────────────────────────────
    if args.ttcat_debug_csv:
        os.environ["TTCAT_DEBUG_CSV"] = "1"
        logger.info("ttcat: debug CSV ENABLED")
        if args.ttcat_debug_csv_path:
            os.environ["TTCAT_DEBUG_CSV_PATH"] = args.ttcat_debug_csv_path
            logger.info(f"ttcat: debug CSV path = {args.ttcat_debug_csv_path}")
        else:
            logger.info("ttcat: debug CSV path = ./ttcat_debug.csv (default)")
    elif args.ttcat_debug_csv_path:
        logger.warning(
            "--ttcat-debug-csv-path was given without --ttcat-debug-csv; "
            "the path will be ignored."
        )

    if args.ttcat_quiet:
        os.environ["TTCAT_QUIET"] = "1"
        logger.info("ttcat: endJob report SUPPRESSED")

    # -------------------------------------------------------------------------
    # Hardcoded Configuration (Default Settings)
    # -------------------------------------------------------------------------
    # To avoid argument parsing errors in CRAB, complex settings are managed here.
    # Modify these values directly if needed.
    
    OUTPUT_DIR  = "."         # Output directory (Always current dir for CRAB compatibility)
    CUT_STRING  = None        # Recommended: Apply cuts inside the module's analyze() function
    POSTFIX     = "_Skim"     # Suffix for split output mode
    COMPRESSION = "LZMA:9"    # Compression algorithm (Use "LZ4:4" for faster testing)
    FRIEND      = False       # Run in friend tree mode
    NO_OUT      = False       # If True, skip writing output file (for debugging)

    # -------------------------------------------------------------------------
    # Validation & Loading
    # -------------------------------------------------------------------------

    # 1. Validate Branch Selection File
    if os.path.exists(args.branch_selection):
        logger.info(f"Branch Selection File loaded: {args.branch_selection}")
        logger.info(f"  -> Applied to: OUTPUT tree only (input is read in full)")
        # Preview first 3 lines
        try:
            with open(args.branch_selection, 'r') as f:
                head = [next(f).strip() for _ in range(3)]
            logger.info(f"  -> Preview: {head} ...")
        except StopIteration:
            pass
    else:
        logger.error(f"Branch Selection File NOT found: {args.branch_selection}")
        sys.exit(1)

    # 2. Load Modules and Log Details
    active_modules = []
    if args.imports:
        for imp_str in args.imports:
            # Syntax: module_name:list_name (Default list name: 'modules')
            if ':' in imp_str:
                mod_name, list_name = imp_str.split(':')
            else:
                mod_name, list_name = imp_str, 'modules'
            
            try:
                mod = importlib.import_module(mod_name)
                if hasattr(mod, list_name):
                    loaded = getattr(mod, list_name)
                    active_modules.extend(loaded)
                    logger.info(f"Module Loaded: {mod_name} (List Variable: '{list_name}')")
                    
                    # Log internal parameters of loaded modules
                    for idx, m in enumerate(loaded):
                        class_name = m.__class__.__name__
                        logger.info(f"  -> [{idx}] Class: {class_name}")
                        
                        # Inspect and log public attributes (e.g., thresholds)
                        attrs = vars(m)
                        filtered_attrs = {k: v for k, v in attrs.items() if not k.startswith('_')}
                        if filtered_attrs:
                            logger.info(f"     Parameters: {filtered_attrs}")
                else:
                    logger.error(f"Module '{mod_name}' loaded, but list '{list_name}' NOT found.")
                    sys.exit(1)
            except Exception as e:
                logger.error(f"Failed to import module '{mod_name}': {e}")
                sys.exit(1)

    # 3. Validate Input Files
    n_files = len(args.input_files)
    logger.info(f"Input Files Detected: {n_files}")
    if n_files > 0:
        logger.info(f"  -> First file: {args.input_files[0]}")
        if n_files > 1:
            logger.info(f"  -> ... and {n_files - 1} more files.")

    # 4. Confirm Output Strategy
    if args.output_file:
        logger.info(f"Output Strategy: MERGE (Hadd enabled)")
        logger.info(f"  -> Final Target: {args.output_file}")
    else:
        logger.info(f"Output Strategy: SPLIT (One-to-One)")
        logger.info(f"  -> Suffix: {POSTFIX}")

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------
    logger.info("-" * 60)
    logger.info("Initializing PostProcessor engine...")
    
    try:
        p = PostProcessor(
            outputDir=OUTPUT_DIR,
            inputFiles=args.input_files,
            cut=CUT_STRING,
            # ─────────────────────────────────────────────────────────
            # branchsel       = INPUT  tree filter   -> None (= read all)
            # outputbranchsel = OUTPUT tree filter   -> keep/drop file
            #
            # Do NOT pass the keep/drop file as `branchsel`. That would
            # apply `drop *` to the input tree as well and disable
            # GenPart_*/GenJet_*/genTtbarId reads, breaking modules
            # like ttbarCategorizer that need gen-level information.
            # See module docstring of run_postproc.py for full context.
            # ─────────────────────────────────────────────────────────
            branchsel=None,
            outputbranchsel=args.branch_selection,
            modules=active_modules,
            compression=COMPRESSION,
            friend=FRIEND,
            postfix=POSTFIX,
            noOut=NO_OUT,
            justcount=False, # If True, only count events and exit. At the same time, the fwkJobReport variable must be set to False.
            maxEntries=args.max_events,
            firstEntry=args.first_entry,
            jsonInput=args.json_input,
            haddFileName=args.output_file, # Triggers merge if not None
            provenance=True, # Save provenance metadata in the output file
            fwkJobReport=True, # Set "True" for CRAB job
        )
        
        logger.info("Running Event Loop...")
        p.run()
        
        end_time = datetime.datetime.now()
        duration = end_time - start_time
        logger.info(f"✅ Job Finished Successfully at {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"   Total Runtime: {duration}")
        
    except Exception as e:
        logger.exception("❌ Critical Error during PostProcessor execution.")
        sys.exit(1)

if __name__ == "__main__":
    main()
