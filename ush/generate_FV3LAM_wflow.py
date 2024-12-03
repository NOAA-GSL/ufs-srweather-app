#!/usr/bin/env python3

"""
User interface to create an experiment directory consistent with the user-defined ``config.yaml`` 
file.
"""

# pylint: disable=invalid-name

import argparse
import logging
import os
import sys
from pathlib import Path
from stat import S_IXUSR
from string import Template
from textwrap import dedent

from python_utils import (
    list_to_str,
    log_info,
    import_vars,
    export_vars,
    cp_vrfy,
    ln_vrfy,
    mkdir_vrfy,
    mv_vrfy,
    check_for_preexist_dir_file,
    flatten_dict,
)

from check_python_version import check_python_version
from get_crontab_contents import add_crontab_line
from setup import setup

from uwtools.api.config import get_nml_config, get_yaml_config, realize
from uwtools.api.template import render
from uwtools.api import rocoto as uwrocoto


# pylint: disable=too-many-locals,too-many-branches, too-many-statements
def generate_FV3LAM_wflow(
        ushdir,
        logfile: str = "log.generate_FV3LAM_wflow",
        debug: bool = False) -> str:
    """
    Sets up a forecast experiment and creates a workflow (according to the parameters specified 
    in the configuration file)

    Args:
        ushdir  (str) : The full path of the ``ush/`` directory where this script is located
        logfile (str) : The name of the file where logging is written
        debug   (bool): Enable extra output for debugging
    Returns:
        EXPTDIR (str) : The full path of the directory where this experiment has been generated
    """

    # Set up logging to write to screen and logfile
    setup_logging(logfile, debug)

    # Check python version and presence of some non-standard packages
    check_python_version()

    # Note start of workflow generation
    log_info(
        """
        ========================================================================
        Starting experiment generation...
        ========================================================================"""
    )

    # The setup function reads the user configuration file and fills in
    # non-user-specified values from config_defaults.yaml
    expt_config = setup(ushdir, debug=debug)

    #
    # -----------------------------------------------------------------------
    #
    # Set the full path to the experiment's rocoto workflow xml file. This
    # file will be placed at the top level of the experiment directory and
    # then used by rocoto to run the workflow.
    #
    # -----------------------------------------------------------------------
    #
    wflow_xml_fn = expt_config["workflow"]["WFLOW_XML_FN"]
    wflow_xml_fp = os.path.join(
        expt_config["workflow"]["EXPTDIR"],
        wflow_xml_fn,
    )
    #
    # -----------------------------------------------------------------------
    #
    # Create a multiline variable that consists of a yaml-compliant string
    # specifying the values that the jinja variables in the template rocoto
    # XML should be set to.  These values are set either in the user-specified
    # workflow configuration file (EXPT_CONFIG_FN) or in the setup() function
    # called above.  Then call the python script that generates the XML.
    #
    # -----------------------------------------------------------------------
    #
    if expt_config["platform"]["WORKFLOW_MANAGER"] == "rocoto":

        log_info(
            f"""
            Creating rocoto workflow XML file (WFLOW_XML_FP):
              WFLOW_XML_FP = '{wflow_xml_fp}'"""
        )

        rocoto_yaml_fp = expt_config["workflow"]["ROCOTO_YAML_FP"]
        uwrocoto.realize(
            config=rocoto_yaml_fp,
            output_file=wflow_xml_fp,
            )
    #
    # -----------------------------------------------------------------------
    #
    # Create a symlink in the experiment directory that points to the workflow
    # (re)launch script.
    #
    # -----------------------------------------------------------------------
    #
    exptdir = expt_config["workflow"]["EXPTDIR"]
    wflow_launch_script_fp = expt_config["workflow"]["WFLOW_LAUNCH_SCRIPT_FP"]
    wflow_launch_script_fn = expt_config["workflow"]["WFLOW_LAUNCH_SCRIPT_FN"]
    log_info(
        f"""
        Creating symlink in the experiment directory (EXPTDIR) that points to the
        workflow launch script (WFLOW_LAUNCH_SCRIPT_FP):
          EXPTDIR = '{exptdir}'
          WFLOW_LAUNCH_SCRIPT_FP = '{wflow_launch_script_fp}'""",
        verbose=debug,
    )

    with open(wflow_launch_script_fp, "r", encoding="utf-8") as launch_script_file:
        launch_script_content = launch_script_file.read()

    # Stage an experiment-specific launch file in the experiment directory
    template = Template(launch_script_content)

    # The script needs several variables from the workflow and user sections
    template_variables = {
        **expt_config["user"],
        **expt_config["workflow"],
        "valid_vals_BOOLEAN": list_to_str(
            expt_config["constants"]["valid_vals_BOOLEAN"]
        ),
    }
    launch_content = template.safe_substitute(template_variables)

    launch_fp = os.path.join(exptdir, wflow_launch_script_fn)
    with open(launch_fp, "w", encoding="utf-8") as expt_launch_fn:
        expt_launch_fn.write(launch_content)

    os.chmod(launch_fp, os.stat(launch_fp).st_mode | S_IXUSR)

    #
    # -----------------------------------------------------------------------
    #
    # If USE_CRON_TO_RELAUNCH is set to TRUE, add a line to the user's
    # cron table to call the (re)launch script every
    # CRON_RELAUNCH_INTVL_MNTS minutes.
    #
    # -----------------------------------------------------------------------
    #
    # From here on out, going back to setting variables for everything
    # in the flattened expt_config dictionary
    # TODO: Reference all these variables in their respective
    # dictionaries, instead.
    import_vars(dictionary=flatten_dict(expt_config))
    export_vars(source_dict=flatten_dict(expt_config))

    # pylint: disable=undefined-variable
    if USE_CRON_TO_RELAUNCH:
        add_crontab_line(
            called_from_cron=False,
            machine=expt_config["user"]["MACHINE"],
            crontab_line=expt_config["workflow"]["CRONTAB_LINE"],
            exptdir=exptdir,
            debug=debug,
        )

    #
    # Copy or symlink fix files
    #
    if SYMLINK_FIX_FILES:
        log_info(
            f"""
            Symlinking fixed files from system directory (FIXgsm) to a subdirectory (FIXam):
              FIXgsm = '{FIXgsm}'
              FIXam = '{FIXam}'""",
            verbose=debug,
        )

        ln_vrfy(f"""-fsn '{FIXgsm}' '{FIXam}'""")
    else:

        log_info(
            f"""
            Copying fixed files from system directory (FIXgsm) to a subdirectory (FIXam):
              FIXgsm = '{FIXgsm}'
              FIXam = '{FIXam}'""",
            verbose=debug,
        )

        check_for_preexist_dir_file(FIXam, "delete")
        mkdir_vrfy("-p", FIXam)
        mkdir_vrfy("-p", os.path.join(FIXam, "fix_co2_proj"))

        num_files = len(FIXgsm_FILES_TO_COPY_TO_FIXam)
        for i in range(num_files):
            fn = f"{FIXgsm_FILES_TO_COPY_TO_FIXam[i]}"
            cp_vrfy(os.path.join(FIXgsm, fn), os.path.join(FIXam, fn))
    #
    # -----------------------------------------------------------------------
    #
    # Copy MERRA2 aerosol climatology data.
    #
    # -----------------------------------------------------------------------
    #
    if expt_config["workflow"]["USE_MERRA_CLIMO"]:
        log_info(
            f"""
            Copying MERRA2 aerosol climatology data files from system directory
            (FIXaer/FIXlut) to a subdirectory (FIXclim) in the experiment directory:
              FIXaer = '{FIXaer}'
              FIXlut = '{FIXlut}'
              FIXclim = '{FIXclim}'""",
            verbose=debug,
        )

        check_for_preexist_dir_file(FIXclim, "delete")
        mkdir_vrfy("-p", FIXclim)

        if SYMLINK_FIX_FILES:
            ln_vrfy("-fsn", os.path.join(FIXaer, "merra2.aerclim*.nc"), FIXclim)
            ln_vrfy("-fsn", os.path.join(FIXlut, "optics*.dat"), FIXclim)
        else:
            cp_vrfy(os.path.join(FIXaer, "merra2.aerclim*.nc"), FIXclim)
            cp_vrfy(os.path.join(FIXlut, "optics*.dat"), FIXclim)
    #
    # Copy the CCPP physics suite definition file from its location in the
    # clone of the FV3 code repository to the experiment directory (EXPT-
    # DIR).
    #
    log_info(
        """
        Copying the CCPP physics suite definition XML file from its location in
        the forecast model directory structure to the experiment directory...""",
        verbose=debug,
    )
    cp_vrfy(CCPP_PHYS_SUITE_IN_CCPP_FP, CCPP_PHYS_SUITE_FP)
    #
    # Copy the field dictionary file from its location in the
    # clone of the FV3 code repository to the experiment directory (EXPT-
    # DIR).
    #
    log_info(
        """
        Copying the field dictionary file from its location in the
        forecast model directory structure to the experiment
        directory...""",
        verbose=debug,
    )
    cp_vrfy(FIELD_DICT_IN_UWM_FP, FIELD_DICT_FP)

    #
    # -----------------------------------------------------------------------
    #
    # To have a record of how this experiment/workflow was generated, copy
    # the experiment/workflow configuration file to the experiment directo-
    # ry.
    #
    # -----------------------------------------------------------------------
    #
    cp_vrfy(os.path.join(ushdir, EXPT_CONFIG_FN), EXPTDIR)

    #
    # -----------------------------------------------------------------------
    #
    # For convenience, print out the commands that need to be issued on the
    # command line in order to launch the workflow and to check its status.
    # Also, print out the line that should be placed in the user's cron table
    # in order for the workflow to be continually resubmitted.
    #
    # -----------------------------------------------------------------------
    #
    if WORKFLOW_MANAGER == "rocoto":
        wflow_db_fn = f"{os.path.splitext(WFLOW_XML_FN)[0]}.db"
        rocotorun_cmd = f"rocotorun -w {WFLOW_XML_FN} -d {wflow_db_fn} -v 10"
        rocotostat_cmd = f"rocotostat -w {WFLOW_XML_FN} -d {wflow_db_fn} -v 10"

        # pylint: disable=line-too-long
        log_info(
            f"""
            To launch the workflow, change location to the experiment directory
            (EXPTDIR) and issue the rocotrun command, as follows:

              > cd {EXPTDIR}
              > {rocotorun_cmd}

            To check on the status of the workflow, issue the rocotostat command
            (also from the experiment directory):

              > {rocotostat_cmd}

            Note that:

            1) The rocotorun command must be issued after the completion of each
               task in the workflow in order for the workflow to submit the next
               task(s) to the queue.

            2) In order for the output of the rocotostat command to be up-to-date,
               the rocotorun command must be issued immediately before issuing the
               rocotostat command.

            For automatic resubmission of the workflow (say every {CRON_RELAUNCH_INTVL_MNTS} minutes), the
            following line can be added to the user's crontab (use 'crontab -e' to
            edit the cron table):

            */{CRON_RELAUNCH_INTVL_MNTS} * * * * cd {EXPTDIR} && ./launch_FV3LAM_wflow.sh called_from_cron="TRUE"
            """
        )
        # pylint: enable=line-too-long

    # If we got to this point everything was successful: move the log
    # file to the experiment directory.
    mv_vrfy(logfile, EXPTDIR)

    return EXPTDIR


def setup_logging(
    logfile: str = "log.generate_FV3LAM_wflow", debug: bool = False
) -> None:
    """
    Sets up logging, printing high-priority (INFO and higher) messages to screen and printing all
    messages with detailed timing and routine info in the specified text file. If ``debug = True``,
    print all messages to both screen and log file.

    Args:
        logfile (str) : The name of the file where logging information is written
        debug   (bool): Enable extra output for debugging
    Returns:
        None

    """
    logging.getLogger().setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(name)-22s %(levelname)-8s %(message)s")

    fh = logging.FileHandler(logfile, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logging.getLogger().addHandler(fh)
    logging.debug(f"Finished setting up debug file logging in {logfile}")

    # If there are already multiple handlers, that means
    # generate_FV3LAM_workflow was called from another function.
    # In that case, do not change the console (print-to-screen) logging.
    if len(logging.getLogger().handlers) > 1:
        return

    console = logging.StreamHandler()
    if debug:
        console.setLevel(logging.DEBUG)
    else:
        console.setLevel(logging.INFO)
    logging.getLogger().addHandler(console)
    logging.debug("Logging set up successfully")


if __name__ == "__main__":

    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Script for setting up a forecast and creating a workflow"
        "according to the parameters specified in the config file\n"
    )

    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Script will be run in debug mode with more verbose output",
    )
    pargs = parser.parse_args()

    USHdir = os.path.dirname(os.path.abspath(__file__))
    wflow_logfile = f"{USHdir}/log.generate_FV3LAM_wflow"

    # Call the generate_FV3LAM_wflow function defined above to generate the
    # experiment/workflow.
    try:
        expt_dir = generate_FV3LAM_wflow(USHdir, wflow_logfile, pargs.debug)
    except:  # pylint: disable=bare-except
        logging.exception(
            dedent(
                f"""
                *********************************************************************
                FATAL ERROR:
                Experiment generation failed. See the error message(s) printed below.
                For more detailed information, check the log file from the workflow
                generation script: {wflow_logfile}
                *********************************************************************\n
                """
            )
        )
        sys.exit(1)

    # pylint: disable=undefined-variable
    # Note workflow generation completion
    log_info(
        f"""
        ========================================================================

            Experiment generation completed.  The experiment directory is:

              EXPTDIR='{EXPTDIR}'

        ========================================================================
        """
    )
