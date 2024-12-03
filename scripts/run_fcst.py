#!/usr/bin/env python
"""
The run script for run_fcst.
"""

import datetime as dt
import logging
import os
import sys
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path

from uwtools.api.config import get_yaml_config
from uwtools.api.fs import link as uwlink
from uwtools.api.fv3 import FV3
from uwtools.api.logging import use_uwtools_logger
from uwtools.api.template import render
from uwtools.api.upp import UPP


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


def link_files(dest_dir, files):
    """
    Link a given list of files to the destination directory using the same file names.
    """
    for fpath in files:
        path = Path(fpath)
        linkname = dest_dir / path.name
        if linkname.is_symlink():
            linkname.unlink()
        logging.info(f"Linking {linkname} -> {path}")
        linkname.symlink_to(path)


def parse_args(argv):
    """
    Parse arguments for the script.
    """
    parser = ArgumentParser(
        description="Script that runs FV3 via uwtools API.",
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


def run_fcst(config_file, cycle, key_path, member):
    """
    Setup and run the FV3 Driver.
    """
    expt_config = get_yaml_config(config_file)

    # The experiment config will have {{ CRES | env }} and {{ MEMBER | env }} expressions in it that
    # need to be dereferenced during driver initialization
    os.environ["CRES"] = expt_config["workflow"]["CRES"]
    os.environ["MEMBER"] = member

    restart = False
    if restart:
        restart_settings = {
            "fv_core_nml": {
                "external_ic": False,
                "make_nh": False,
                "mountain": True,
                "na_init": 0,
                "nggps_ic": False,
                "warm_start": True,
            },
            "gfs_physics_nml": {
                "nstf_name": [2, 0, 0, 0, 0],
            },
        }
        expt_config.update_from(
            {
                "task_run_fcst": {
                    "fv3": {"namelist": {"update_values": restart_settings}}
                }
            }
        )

    fv3_driver = FV3(
        config=expt_config,
        cycle=cycle,
        key_path=key_path,
    )
    rundir = Path(fv3_driver.config["rundir"])

    if expt_config["cpl_aqm_parm"]["CPL_AQM"]:
        restart_overrides = {}
        if restart:
            restart_overrides["init_concentrations"] = False

        # Prepare the rc file from a template
        aqm_block = _walk_key_path(expt_config, key_path + ["aqm"])
        render(
            input_file=aqm_block["template_file"],
            output_file=rundir / "aqm.rc",
            overrides=restart_overrides,
            values_src=aqm_block["template_values"],
        )

    # Prepare config files for inline post, if needed
    model_configure_block = _walk_key_path(
        fv3_driver.config,
        ["model_configure", "update_values"],
    )
    if do_post := model_configure_block["write_dopost"]:
        upp_driver = UPP(
            config=expt_config,
            cycle=cycle,
            leadtime=dt.timedelta(hours=999),
            key_path=key_path,
        )
        upp_driver.control_file()
        upp_driver.files_copied()
        upp_driver.files_linked()
        upp_driver.namelist_file()

    ufs_configure_block = _walk_key_path(expt_config, key_path + ["ufs_configure"])
    render(
        input_file=ufs_configure_block["template_file"],
        output_file=rundir / "ufs.configure",
        values_src=ufs_configure_block["template_values"],
    )

    # Run the FV3 program via UW driver
    logging.info(f"Will run FV3 in {rundir}")
    fv3_driver.run()

    if not (rundir / "runscript.fv3.done").is_file():
        logging.error("Error occurred running FV3. Please see component error logs.")
        sys.exit(1)

    # Deliver output data
    if do_post:
        fcst_len = fv3_driver.config["length"]
        output_fh = fv3_driver.config["model_configure"]["update_values"][
            "output_fh"
        ].split()
        if len(output_fh) == 2 and output_fh[-1] == -1:
            expected_output_hours = range(0, fcst_len + 1, int(output_fh[0]))
        else:
            expected_output_hours = [int(x) for x in output_fh]

        upp_config = _walk_key_path(expt_config, ["task_run_post"])

        for fcst_hr in expected_output_hours:
            links = {}
            for label in upp_config["output_file_labels"]:
                # deepcopy here because desired_output_name is parameterized within the loop
                expt_config_cp = get_yaml_config(deepcopy(expt_config.data))
                expt_config_cp.dereference(
                    context={
                        "cycle": cycle,
                        "leadtime": dt.timedelta(hours=fcst_hr),
                        "file_label": label,
                        **expt_config_cp,
                    }
                )
                upp_block = _walk_key_path(expt_config_cp, ["task_run_post"])
                desired_output_fn = upp_block["desired_output_name"]
                upp_output_fn = rundir / f"{label.upper()}.GrbF{fcst_hr:02d}"
                links[desired_output_fn] = str(upp_output_fn)

            uwlink(target_dir=rundir / "postprd", config=links)


if __name__ == "__main__":

    use_uwtools_logger()

    args = parse_args(sys.argv[1:])
    run_fcst(
        config_file=args.config_file,
        cycle=args.cycle,
        key_path=args.key_path,
        member=args.member,
    )
