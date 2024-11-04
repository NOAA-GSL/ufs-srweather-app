#!/usr/bin/env python
"""
The run script for chgres_cube for both initial and lateral boundary conditions.
"""

import datetime as dt
import logging
import os
import sys
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path

from uwtools.api.chgres_cube import ChgresCube
from uwtools.api.config import get_yaml_config
from uwtools.api.fs import copy as uwcopy
from uwtools.api.logging import use_uwtools_logger


def _deliver_files(config, dst_dir, key_path, src_dir):
    """
    Deliver files defined in the config "outuput_file_links" section.
    """
    dst_dir.mkdir(exist_ok=True)
    output_links = _walk_key_path(config, key_path + ["output_file_links"])
    output_links = {k: str(src_dir / v) for k, v in output_links.items()}
    if not uwcopy(target_dir=dst_dir, config=output_links):
        logging.error("Files could not be copied to their final destination.")
        sys.exit(1)


def _get_external_fns(config, cycle, key_path):
    """
    Return external model file names and forecast hours for the given task in the experiment.

    They come from the metadata file written by the prior data retrival task.
    """
    config_cp = get_yaml_config(deepcopy(config.data))
    config_cp.dereference(
        context={
            **config_cp,
            **os.environ,
            "cycle": cycle,
        }
    )
    varsfilepath = _walk_key_path(config_cp, key_path)["input_files_metadata_path"]
    external_config = get_yaml_config(varsfilepath)
    external_config_fns = external_config["external_model_fns"]
    external_config_fhrs = external_config["external_model_fhrs"]
    return external_config_fhrs, external_config_fns


def _is_grib2(config, key_path):
    """
    Is the input in grib2 format?
    """
    return (
        _walk_key_path(
            config,
            key_path + ["chgres_cube", "namelist", "update_values", "config"],
        ).get("input_type")
        == "grib2"
    )


def _walk_key_path(config, key_path):
    """
    Navigate to the sub-config at the end of the path of given keys.
    """
    keys = []
    pathstr = "<unknown>"
    for key in key_path:
        keys.append(key)
        pathstr = " -> ".join(keys)
        try:
            subconfig = config[key]
        except KeyError:
            logging.error(f"Bad config path: {pathstr}")
            raise
        if not isinstance(subconfig, dict):
            logging.error(f"Value at {pathstr} must be a dictionary")
            sys.exit(1)
        config = subconfig
    return config


def parse_args(argv):
    """
    Parse arguments for the script.
    """
    parser = ArgumentParser(
        description="Script that runs chgres_cube via uwtools API for SRW",
    )
    parser.add_argument(
        "-c",
        "--config-file",
        metavar="PATH",
        required=True,
        help="Path to experiment config file.",
        type=Path,
    )
    parser.add_argument(
        "--cycle",
        help="The cycle in ISO8601 format (e.g. 2024-07-15T18).",
        required=True,
        type=dt.datetime.fromisoformat,
    )
    parser.add_argument(
        "--key-path",
        help="Dot-separated path of keys leading through the config to the driver's YAML block.",
        metavar="KEY[.KEY...]",
        required=True,
        type=lambda s: s.split("."),
    )
    parser.add_argument(
        "--member",
        default="000",
        help="The 3-digit ensemble member number.",
    )
    return parser.parse_args(argv)


def run_chgres_cube(config_file, cycle, key_path, member):
    """
    Setup and run the chgres_cube UW Driver.
    """
    expt_config = get_yaml_config(config_file)

    # The experiment config will have {{ 'CRES' | env }} expressions in it that need to be
    # dereferenced during driver initialization.
    os.environ["CRES"] = expt_config["workflow"]["CRES"]
    os.environ["MEMBER"] = member

    ext_fhrs, ext_fns = _get_external_fns(expt_config, cycle, key_path)
    grib2_input = _is_grib2(expt_config, key_path)
    if "task_make_ics" in key_path:
        if grib2_input:
            os.environ["fn_grib2"] = ext_fns[0]
        else:
            os.environ["fn_atm"] = ext_fns[0]
            os.environ["fn_sfc"] = ext_fns[1]

        driver = run_driver(
            ChgresCube, config_file, cycle, key_path, leadtime=dt.timedelta(hours=0)
        )
        rundir = Path(driver.config["rundir"])

        # Deliver output data to the forecast's INPUT dir.
        delivery_dir = rundir.parent / "INPUT"
        expt_config_cp = get_yaml_config(deepcopy(expt_config.data))
        expt_config_cp.dereference(
            context={
                **expt_config_cp,
                **os.environ,
                "cycle": cycle,
            }
        )
        _deliver_files(
            config=expt_config_cp,
            dst_dir=delivery_dir,
            key_path=key_path,
            src_dir=rundir,
        )

    else:  # Loop over make_lbcs tasks.
        # This loop will need a version of the config that is not dereferenced.
        expt_config = get_yaml_config(config_file)
        for external_fhr, external_fn in list(zip(ext_fhrs, ext_fns)):
            os.environ["fn_grib2" if grib2_input else "fn_atm"] = external_fn

            # Determine lead time and run the driver
            lbc_offset_fhrs = _walk_key_path(
                expt_config, ["task_get_extrn_lbcs", "envvars"]
            )["EXTRN_MDL_LBCS_OFFSET_HRS"]
            leadtime = dt.timedelta(hours=int(external_fhr) - int(lbc_offset_fhrs))
            driver = run_driver(
                ChgresCube, config_file, cycle, key_path, leadtime=leadtime
            )
            rundir = Path(driver.config["rundir"])

            # Use a copy of the original here to avoid opening the file every time.
            expt_config_cp = get_yaml_config(deepcopy(expt_config.data))

            # This dereferencing must be inside loop bc the fcst hour is set differently each time.
            expt_config_cp.dereference(
                context={
                    **expt_config_cp,
                    **os.environ,
                    "cycle": cycle,
                    "leadtime": leadtime,
                }
            )
            # Deliver output data to the forecast's INPUT dir.
            delivery_dir = rundir.parent / "INPUT"
            _deliver_files(
                config=expt_config_cp,
                dst_dir=delivery_dir,
                key_path=key_path,
                src_dir=rundir,
            )


def run_driver(driver_obj, config_file, cycle, key_path, leadtime):
    """
    Initialize and run the provided UW driver.

    Return the configured object.
    """
    driver = driver_obj(
        config=config_file,
        cycle=cycle,
        key_path=key_path,
        leadtime=leadtime,
    )
    rundir = Path(driver.config["rundir"])
    logging.info(f"Will run {driver.driver_name()} in {rundir}")
    driver.run()

    if not (rundir / f"runscript.{driver.driver_name()}.done").is_file():
        logging.error(
            f"Error occurred running {driver.driver_name()}. Please see component error logs."
        )
        sys.exit(1)
    return driver


if __name__ == "__main__":

    use_uwtools_logger()

    args = parse_args(sys.argv[1:])
    run_chgres_cube(
        config_file=args.config_file,
        cycle=args.cycle,
        key_path=args.key_path,
        member=args.member,
    )
