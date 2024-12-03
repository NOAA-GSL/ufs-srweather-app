#!/usr/bin/env python3

import copy
import datetime
import glob
import logging
import json
import os
import re
import sys
import traceback
from pathlib import Path
from textwrap import dedent

import yaml
from uwtools.api.config import get_nml_config, get_yaml_config

from link_fix import link_fix
from python_utils import (
    log_info,
    cd_vrfy,
    date_to_str,
    mkdir_vrfy,
    rm_vrfy,
    check_var_valid_value,
    lowercase,
    uppercase,
    list_to_str,
    check_for_preexist_dir_file,
    flatten_dict,
    check_structure_dict,
    update_dict,
    import_vars,
    get_env_var,
    load_config_file,
    cfg_to_shell_str,
    cfg_to_yaml_str,
    load_ini_config,
    get_ini_value,
    str_to_list,
    extend_yaml,
    has_tag_with_value,
    load_xml_file,
)

from set_cycle_dates import set_cycle_dates
from set_predef_grid_params import set_predef_grid_params
from set_gridparams_ESGgrid import set_gridparams_ESGgrid
from set_gridparams_GFDLgrid import set_gridparams_GFDLgrid
from uwtools.api.config import get_yaml_config, validate


def load_config_for_setup(ushdir, default_config_path, user_config_path):
    """Load in the default, machine, and user configuration files into
    Python dictionaries. Return the combined experiment dictionary.

    Args:
      ushdir             (str): Path to the ``ush`` directory for the SRW App
      default_config     (str): Path to ``config_defaults.yaml``
      user_config        (str): Path to the user-provided config YAML (usually named 
                                ``config.yaml``)

    Returns:
        None
    
    Raises:
        FileNotFoundError: If the user-provided configuration file or the machine file does not 
                           exist.
        Exception: If (1) the user-provided configuration file cannot be loaded or (2) it contains 
                   invalid sections/keys or (3) it does not contain mandatory information or (4) 
                   an invalid datetime format is used. 
    """

    ushdir = Path(ushdir)

    # Load the default and user configs.
    logging.debug(f"Loading config defaults file {default_config_path}")
    default_config = get_yaml_config(default_config_path)
    logging.debug(f"Read in the following values from config defaults file:\n")
    logging.debug(default_config)

    user_config = get_yaml_config(user_config_path)
    logging.debug(f"Read in the following values from YAML config file {user_config}:\n")
    logging.debug(user_config)

    # Make sure the keys in user config match those in the default
    # config. Skipping during uwtools integration activities.
    invalid = {}

    # Task and metatask entries can be added arbitrarily under the
    # rocoto section. Remove those from invalid if they exist.
    for key in invalid.copy().keys():
        if key.split("_", maxsplit=1)[0] in ["task", "metatask"]:
            invalid.pop(key)
            logging.info(f"Found and allowing key {key}")

    if invalid:
        errmsg = f"Invalid key(s) specified in {user_config}:\n"
        for entry in invalid:
            errmsg = errmsg + f"{entry} = {invalid[entry]}\n"
        errmsg = (
            errmsg + f"\nCheck {default_config} for allowed user-specified variables\n"
        )
        raise Exception(errmsg)

    # Mandatory variables *must* be set in the user's config; the default value is invalid
    mandatory = ["user.MACHINE"]
    for val in mandatory:
        sect, key = val.split(".")
        user_setting = user_config.get(sect, {}).get(key)
        if user_setting is None:
            raise Exception(
                f"""Mandatory variable "{val}" not found in
            user config file {user_config}"""
            )

    # Load the machine config file
    machine = uppercase(user_config.get("user").get("MACHINE"))
    user_config["user"]["MACHINE"] = uppercase(machine)

    machine_file = ushdir / "machine" / f"{lowercase(machine)}.yaml"

    if not machine_file.exists():
        raise FileNotFoundError(
            dedent(
                f"""
            The machine file {machine_file} does not exist.
            Check that you have specified the correct machine
            ({machine}) in your config file {user_config}"""
            )
        )
    logging.debug(f"Loading machine defaults file {machine_file}")
    machine_config = get_yaml_config(machine_file)

    # Load the fixed files configuration
    fix_file_config = get_yaml_config( ushdir.parent / "parm" / "fixed_files_mapping.yaml")

    # Load the constants file
    constants = get_yaml_config(ushdir / "constants.yaml")

    # Load the rocoto workflow default file
    default_workflow = Path(ushdir).parent / "parm" / "wflow" / "default_workflow.yaml"
    workflow_config = get_yaml_config(default_workflow)

    # Update default config with other loaded config file. Order matters.
    for cfg in (constants, workflow_config, machine_config, fix_file_config,
            user_config):
        default_config.update_from(cfg)

    # Load one more if running Coupled AQM
    if default_config['cpl_aqm_parm']['CPL_AQM']:
        aqm_config = get_yaml_config(ushdir / "config_defaults_aqm.yaml")
        default_config.update_from(aqm_config)

    # Load CCPP suite-specific settings
    ccpp_suite = default_config['workflow']['CCPP_PHYS_SUITE']
    ccpp_config = get_yaml_config(ushdir / "ccpp_suites_defaults.yaml").get(ccpp_suite, {})

    # Create a temporary section containing the physics-based namelist settings
    srw_base_file = Path(ushdir).parent / "parm" / "input.nml.FV3"
    srw_nml = get_nml_config(srw_base_file)
    if ccpp_config:
        # Check to see if there are namelist updates
        try:
            nml_config = ccpp_config["fv3_namelist_settings"]
        except KeyError:
            logging.info(f"No updates for the namelist for suite {ccpp_suite}")
            nml_config = {}

        nml_config = get_nml_config(nml_config)
        srw_nml.update_from(nml_config)
    default_config.update_from({"fv3_namelist_settings": get_yaml_config(srw_nml.data).data})

    # Apply any suite changes not related to the model namelist
    ccpp_config.pop("fv3_namelist_settings", None)
    default_config.update_from(ccpp_config)


    # Load external model-specific settings
    external_cfg = get_yaml_config(ushdir / "external_model_defaults.yaml")
    for bcs in ("ics", "lbcs"):
        get_task_config = default_config[f"task_get_extrn_{bcs}"]
        external_model = get_task_config["envvars"][f"EXTRN_MDL_NAME_{bcs.upper()}"]
        bcs_task = f"task_make_{bcs}"
        default_config.update_from(
            {bcs_task: external_cfg.get(external_model, {}).get(bcs_task, {}) }
        )

    # Load stochastic physics params
    stochastic_params = get_yaml_config(Path(ushdir, "stochastic_params.yaml"))
    fcst_config = default_config["task_run_fcst"]["fv3"]
    fcst_nml_config = get_yaml_config(fcst_config["namelist"]["update_values"])
    for switch_name in ("do_spp", "do_sppt", "do_shum", "do_skeb", "do_lsm_spp"):
        if default_config["global"][switch_name.upper()]:
            fcst_nml_config.update_from(stochastic_params.get(switch_name))

    # Set "Home" directory, the top-level ufs-srweather-app directory
    homedir = Path(__file__).parent.parent.resolve()
    default_config["user"]["HOMEdir"] = str(homedir)

    # Special logic if EXPT_BASEDIR is a relative path; see config_defaults.yaml for explanation
    expt_basedir = default_config["workflow"]["EXPT_BASEDIR"]
    if (not expt_basedir) or (expt_basedir[0] != "/"):
        expt_basedir = homedir.parent / "expt_dirs" / expt_basedir
    default_config["workflow"]["EXPT_BASEDIR"] = str(Path(expt_basedir).resolve())

    # Expand out the workflow tasks now that all settings have been applied
    taskgroups = default_config["workflow"]["taskgroups"]
    default_config["rocoto"]["tasks"] = {}
    for taskgroup in taskgroups:
        tasks = get_yaml_config(homedir / taskgroup)
        keep = {k: v for k, v in tasks.items() if not re.search(r"^default_*", k)}
        default_config["rocoto"]["tasks"].update(keep)

    # Update one more time in case there are user or machine settings to override the tasks
    for cfg in (machine_config, user_config):
        default_config.update_from(cfg)

    # Dereference all Jinja expressions
    default_config.dereference()

    # Validate experiment config against schema
    schema = ushdir / "experiment.jsonschema"
    valid = validate(schema_file=schema, config=default_config)

    if not valid:
        logging.error(f"Experiment configuration is not valid against schema")
        sys.exit(1)

    return default_config


def set_srw_paths(ushdir, expt_config):
    """
    Generates a dictionary of directories that describe the SRW App
    structure, i.e., where the SRW App is installed and the paths to
    external repositories managed via the ``manage_externals`` tool.

    Other paths for the SRW App are set as defaults in ``config_defaults.yaml``.

    Args:
        ushdir      (str) : Path to the system location of the ``ush`` directory under the 
                            SRW App clone
        expt_config (dict): Contains the configuration settings for the user-defined experiment

    Returns:
        Dictionary of configuration settings and system paths as keys/values
    
    Raises:
        KeyError: If the external repository required is not listed in the externals 
                  configuration file (e.g., ``Externals.cfg``)
        FileNotFoundError: If the ``ufs-weather-model`` code containing the FV3 source code has 
                           not been cloned properly
    """

    # HOMEdir is the location of the SRW clone, one directory above ush/
    homedir = expt_config.get("user", {}).get("HOMEdir")

    # Read Externals.cfg
    mng_extrns_cfg_fn = os.path.join(homedir, "Externals.cfg")
    try:
        mng_extrns_cfg_fn = os.readlink(mng_extrns_cfg_fn)
    except:
        pass
    cfg = load_ini_config(mng_extrns_cfg_fn)

    # Get the base directory of the FV3 forecast model code.
    external_name = expt_config.get("workflow", {}).get("FCST_MODEL")
    property_name = "local_path"

    try:
        ufs_wthr_mdl_dir = get_ini_value(cfg, external_name, property_name)
    except KeyError:
        errmsg = dedent(
            f"""
            Externals configuration file {mng_extrns_cfg_fn}
            does not contain '{external_name}'."""
        )
        raise Exception(errmsg) from None

    # Check that the model code has been downloaded
    ufs_wthr_mdl_dir = os.path.join(homedir, ufs_wthr_mdl_dir)
    if not os.path.exists(ufs_wthr_mdl_dir):
        raise FileNotFoundError(
            dedent(
                f"""
                The base directory in which the FV3 source code should be located
                (UFS_WTHR_MDL_DIR) does not exist:
                  UFS_WTHR_MDL_DIR = '{ufs_wthr_mdl_dir}'
                Please clone the external repository containing the code in this directory,
                build the executable, and then rerun the workflow."""
            )
        )

    return dict(
        USHdir=ushdir,
        UFS_WTHR_MDL_DIR=ufs_wthr_mdl_dir,
    )


def setup(USHdir, user_config_fn="config.yaml", debug: bool = False):
    """Validates user-provided configuration settings and derives
    a secondary set of parameters needed to configure a Rocoto-based SRW App
    workflow. The secondary parameters are derived from a set of required
    parameters defined in ``config_defaults.yaml``, a user-provided
    configuration file (e.g., ``config.yaml``), or a YAML machine file.

    A set of global variable definitions is saved to the experiment
    directory as a bash configure file that is sourced by scripts at run
    time.

    Args:
        USHdir          (str): The full path of the ``ush/`` directory where this script 
                               (``setup.py``) is located
        user_config_fn  (str): The name of a user-provided configuration YAML (usually 
                               ``config.yaml``)
        debug          (bool): Enable extra output for debugging

    Returns:
        None
    
    Raises: 
        ValueError: If checked configuration values are invalid (e.g., forecast length, 
                    ``EXPTDIR`` path)
        FileExistsError: If ``EXPTDIR`` already exists, and ``PREEXISTING_DIR_METHOD`` is not 
                         set to a compatible handling method
        FileNotFoundError: If the path to a particular file does not exist or if the file itself 
                           does not exist at the expected path
        TypeError: If ``USE_CUSTOM_POST_CONFIG_FILE`` or ``USE_CRTM`` are set to true but no 
                   corresponding custom configuration file or CRTM fix file directory is set
        KeyError: If an invalid value is provided (i.e., for ``GRID_GEN_METHOD``)
    """

    logger = logging.getLogger(__name__)

    # print message
    log_info(
        f"""
        ========================================================================
        Starting function setup() in \"{os.path.basename(__file__)}\"...
        ========================================================================"""
    )

    # Create a dictionary of config options from defaults, machine, and
    # user config files.
    default_config_fp = os.path.join(USHdir, "config_defaults.yaml")
    user_config_fp = os.path.join(USHdir, user_config_fn)
    expt_config = load_config_for_setup(USHdir, default_config_fp, user_config_fp)

    # Set up some paths relative to the SRW clone
    expt_config["user"].update(set_srw_paths(USHdir, expt_config))
    expt_config.dereference()

    #
    # -----------------------------------------------------------------------
    #
    # Validate the experiment configuration starting with the workflow,
    # then in rough order of the tasks in the workflow
    #
    # -----------------------------------------------------------------------
    #

    # Workflow
    workflow_config = expt_config["workflow"]

    workflow_id = workflow_config["WORKFLOW_ID"]
    log_info(f"""WORKFLOW ID = {workflow_id}""")

    debug = workflow_config.get("DEBUG")
    if debug:
        log_info(
            """
            Setting VERBOSE to \"TRUE\" because DEBUG has been set to \"TRUE\"..."""
        )
        workflow_config["VERBOSE"] = True

    verbose = workflow_config["VERBOSE"]

    # The forecast length (in integer hours) cannot contain more than 3 characters.
    # Thus, its maximum value is 999.
    fcst_len_hrs_max = 999
    fcst_len_hrs = workflow_config.get("FCST_LEN_HRS")
    if fcst_len_hrs > fcst_len_hrs_max:
        raise ValueError(
            f"""
            Forecast length is greater than maximum allowed length:
              FCST_LEN_HRS = {fcst_len_hrs}
              fcst_len_hrs_max = {fcst_len_hrs_max}"""
        )

    #
    # -----------------------------------------------------------------------
    #
    # Set the full path to the experiment directory.  Then check if it already
    # exists and if so, deal with it as specified by PREEXISTING_DIR_METHOD.
    #
    # -----------------------------------------------------------------------
    #

    expt_subdir = workflow_config.get("EXPT_SUBDIR", "")
    exptdir = workflow_config.get("EXPTDIR")

    # Update some paths that include EXPTDIR and EXPT_BASEDIR
    expt_config.dereference()
    preexisting_dir_method = workflow_config.get("PREEXISTING_DIR_METHOD", "")
    try:
        check_for_preexist_dir_file(exptdir, preexisting_dir_method)
    except ValueError:
        logger.exception(
            f"""
            Check that the following values are valid:
            EXPTDIR {exptdir}
            PREEXISTING_DIR_METHOD {preexisting_dir_method}
            """
        )
        raise
    except FileExistsError:
        errmsg = dedent(
            f"""
            EXPTDIR ({exptdir}) already exists, and PREEXISTING_DIR_METHOD = {preexisting_dir_method}

            To ignore this error, delete the directory, or set 
            PREEXISTING_DIR_METHOD = delete, or
            PREEXISTING_DIR_METHOD = rename
            in your config file.
            """
        )
        raise FileExistsError(errmsg) from None

    #
    # -----------------------------------------------------------------------
    #
    # Set cron table entry for relaunching the workflow if
    # USE_CRON_TO_RELAUNCH is set to TRUE.
    #
    # -----------------------------------------------------------------------
    #
    if workflow_config.get("USE_CRON_TO_RELAUNCH"):
        intvl_mnts = workflow_config.get("CRON_RELAUNCH_INTVL_MNTS")
        launch_script_fn = workflow_config.get("WFLOW_LAUNCH_SCRIPT_FN")
        launch_log_fn = workflow_config.get("WFLOW_LAUNCH_LOG_FN")
        workflow_config["CRONTAB_LINE"] = (
            f"""*/{intvl_mnts} * * * * cd {exptdir} && """
            f"""./{launch_script_fn} called_from_cron="TRUE" >> ./{launch_log_fn} 2>&1"""
        )
    #
    # -----------------------------------------------------------------------
    #
    # Check user settings against platform settings
    #
    # -----------------------------------------------------------------------
    #

    platform_config = expt_config["platform"]
    rocoto_config = expt_config.get("rocoto", {})
    rocoto_tasks = rocoto_config.get("tasks")
    run_make_grid = rocoto_tasks.get("task_make_grid") is not None
    run_make_orog = rocoto_tasks.get("task_make_orog") is not None
    run_make_sfc_climo = rocoto_tasks.get("task_make_sfc_climo") is not None

    # Necessary tasks are turned on
    pregen_basedir = platform_config.get("DOMAIN_PREGEN_BASEDIR")
    if pregen_basedir is None and not (
        run_make_grid and run_make_orog and run_make_sfc_climo
    ):
        raise Exception(
            f"""
            DOMAIN_PREGEN_BASEDIR must be set when any of the following
            tasks are not included in the workflow:
                RUN_MAKE_GRID = {run_make_grid}
                RUN_MAKE_OROG = {run_make_orog}
                RUN_MAKE_SFC_CLIMO = {run_make_sfc_climo}"""
        )

    # A batch system account is specified
    if platform_config.get("WORKFLOW_MANAGER") is not None:
        if not expt_config.get("user").get("ACCOUNT"):
            raise Exception(
                dedent(
                    f"""
                  ACCOUNT must be specified in config or machine file if using a workflow manager.
                  WORKFLOW_MANAGER = {platform_config.get("WORKFLOW_MANAGER")}\n"""
                )
            )

    def _remove_tag(tasks, tag):
        """ Remove the tag for all the tasks in the workflow """

        if not isinstance(tasks, dict):
            return
        for task, task_settings in tasks.items():
            task_type = task.split("_", maxsplit=1)[0]
            if task_type == "task":
                task_settings.pop(tag, None)
            elif task_type == "metatask":
                _remove_tag(task_settings, tag)

    # Remove all memory tags for platforms that do not support them
    remove_memory = platform_config.get("REMOVE_MEMORY")
    if remove_memory:
        _remove_tag(rocoto_tasks, "memory")

    for part in ["PARTITION_HPSS", "PARTITION_DEFAULT", "PARTITION_FCST"]:
        partition = platform_config.get(part)
        if not partition:
            _remove_tag(rocoto_tasks, 'partition')

    # When not running subhourly post, remove those tasks, if they exist
    if not expt_config.get("task_run_post", {}).get("SUB_HOURLY_POST"):
        post_meta = rocoto_tasks.get("metatask_run_ens_post", {})
        post_meta.pop("metatask_run_sub_hourly_post", None)
        post_meta.pop("metatask_sub_hourly_last_hour_post", None)
    #
    # -----------------------------------------------------------------------
    #
    # Remove all verification [meta]tasks for which no fields are specified.
    #
    # -----------------------------------------------------------------------
    #
    vx_fields_all = {}
    vx_metatasks_all = {}

    vx_fields_all["CCPA"] = ["APCP"]
    vx_metatasks_all["CCPA"] = [
        "metatask_PcpCombine_obs",
        "metatask_PcpCombine_fcst_APCP_all_accums_all_mems",
        "metatask_GridStat_CCPA_all_accums_all_mems",
        "metatask_GenEnsProd_EnsembleStat_CCPA",
        "metatask_GridStat_CCPA_ensmeanprob_all_accums",
    ]

    vx_fields_all["NOHRSC"] = ["ASNOW"]
    vx_metatasks_all["NOHRSC"] = [
        "task_get_obs_nohrsc",
        "metatask_PcpCombine_fcst_ASNOW_all_accums_all_mems",
        "metatask_GridStat_NOHRSC_all_accums_all_mems",
        "metatask_GenEnsProd_EnsembleStat_NOHRSC",
        "metatask_GridStat_NOHRSC_ensmeanprob_all_accums",
    ]

    vx_fields_all["MRMS"] = ["REFC", "RETOP"]
    vx_metatasks_all["MRMS"] = [
        "metatask_GridStat_MRMS_all_mems",
        "metatask_GenEnsProd_EnsembleStat_MRMS",
        "metatask_GridStat_MRMS_ensprob",
    ]

    vx_fields_all["NDAS"] = ["ADPSFC", "ADPUPA"]
    vx_metatasks_all["NDAS"] = [
        "task_run_MET_Pb2nc_obs",
        "metatask_PointStat_NDAS_all_mems",
        "metatask_GenEnsProd_EnsembleStat_NDAS",
        "metatask_PointStat_NDAS_ensmeanprob",
    ]

    # Get the vx fields specified in the experiment configuration.
    vx_fields_config = expt_config["verification"]["VX_FIELDS"]

    # If there are no vx fields specified, remove those tasks that are necessary
    # for all observation types.
    if not vx_fields_config:
        metatask = "metatask_check_post_output_all_mems"
        rocoto_config["tasks"].pop(metatask)

    # If for a given obstype no fields are specified, remove all vx metatasks
    # for that obstype.
    for obstype in vx_fields_all:
        vx_fields_obstype = [
            field for field in vx_fields_config if field in vx_fields_all[obstype]
        ]
        if not vx_fields_obstype:
            for metatask in vx_metatasks_all[obstype]:
                if metatask in rocoto_config["tasks"]:
                    logging.info(
                        dedent(
                            f"""
                        Removing verification [meta]task
                          "{metatask}"
                        from workflow since no fields belonging to observation type "{obstype}"
                        are specified for verification."""
                        )
                    )
                    rocoto_config["tasks"].pop(metatask)

    #
    # -----------------------------------------------------------------------
    #
    # ICS and LBCS settings and validation
    #
    # -----------------------------------------------------------------------
    #
    def _get_location(xcs, fmt, expt_cfg):
        ics_lbcs = expt_cfg.get("data", {}).get("ics_lbcs")
        if ics_lbcs is not None:
            v = ics_lbcs.get(xcs)
            if not isinstance(v, dict):
                return v
            else:
                return v.get(fmt, "")
        else:
            return ""

    # Get the paths to any platform-supported data streams
    get_extrn_ics = expt_config.get("task_get_extrn_ics", {}).get("envvars")
    extrn_mdl_sysbasedir_ics = _get_location(
        get_extrn_ics.get("EXTRN_MDL_NAME_ICS"),
        get_extrn_ics.get("FV3GFS_FILE_FMT_ICS"),
        expt_config,
    )
    get_extrn_ics["EXTRN_MDL_SYSBASEDIR_ICS"] = extrn_mdl_sysbasedir_ics

    get_extrn_lbcs = expt_config.get("task_get_extrn_lbcs", {}).get("envvars")
    extrn_mdl_sysbasedir_lbcs = _get_location(
        get_extrn_lbcs.get("EXTRN_MDL_NAME_LBCS"),
        get_extrn_lbcs.get("FV3GFS_FILE_FMT_LBCS"),
        expt_config,
    )
    get_extrn_lbcs["EXTRN_MDL_SYSBASEDIR_LBCS"] = extrn_mdl_sysbasedir_lbcs

    # remove the data key -- it's not needed beyond this point
    if "data" in expt_config:
        expt_config.pop("data")

    # Check for the user-specified directories for external model files if
    # USE_USER_STAGED_EXTRN_FILES is set to TRUE
    task_keys = zip(
        [get_extrn_ics, get_extrn_lbcs],
        ["EXTRN_MDL_SOURCE_BASEDIR_ICS", "EXTRN_MDL_SOURCE_BASEDIR_LBCS"],
    )

    for task, data_key in task_keys:
        use_staged_extrn_files = task.get("USE_USER_STAGED_EXTRN_FILES")
        if use_staged_extrn_files:
            basedir = task[data_key]
            # Check for the base directory up to the first templated field.
            idx = basedir.find("$")
            if idx == -1:
                idx = len(basedir)

            if not os.path.exists(basedir[:idx]):
                raise FileNotFoundError(
                    f'''
                    The user-staged-data directory does not exist.
                    Please point to the correct path where your external
                    model files are stored.
                      {data_key} = \"{basedir}\"'''
                )

    # Make sure the vertical coordinate file for both make_lbcs and
    # make_ics is the same.
    vcoord_files = {}
    for bcs_task in ("task_make_ics", "task_make_lbcs"):
        vcoord_files[bcs_task] = (
            expt_config[bcs_task]["chgres_cube"]["namelist"]
            .get("config", {})
            .get("vcoord_file_target_grid")
        )

    if not all(x == list(vcoord_files.values())[0] for x in vcoord_files.values()):
        raise ValueError(
            f"""
            The VCOORD_FILE must be set to the same value for both the
            make_ics task and the make_lbcs task. They are currently
            set to:

            {json.dumps(vcoord_files)}
            """
        )

    #
    # -----------------------------------------------------------------------
    #
    # Forecast settings
    #
    # -----------------------------------------------------------------------
    #

    fcst_config = expt_config["task_run_fcst"]
    grid_config = expt_config["task_make_grid"]
    ccpp_suite = workflow_config["CCPP_PHYS_SUITE"]

    # Warn if user has specified a large timestep inappropriately
    hires_ccpp_suites = ["FV3_RRFS_v1beta", "FV3_WoFS_v0", "FV3_HRRR"]
    if ccpp_suite in hires_ccpp_suites:
        dt = fcst_config.get("DT_ATMOS")
        if dt:
            if dt > 40:
                logger.warning(
                    dedent(
                        f"""
                    WARNING: CCPP suite {ccpp_suite} requires short
                    time step regardless of grid resolution. The user-specified value
                    DT_ATMOS = {fcst_config.get("DT_ATMOS")}
                    may result in CFL violations or other errors!
                    """
                    )
                )

    quilting = fcst_config["fv3"]["model_configure"]["update_values"]["quilting"]
    # Gather the pre-defined grid parameters, if needed
    if predef_grid_name := (workflow_config.get("PREDEF_GRID_NAME")):
        grid_params = set_predef_grid_params(
            USHdir,
            workflow_config["PREDEF_GRID_NAME"],
            quilting,
        )
        # Users like to change these variables, so don't overwrite them
        special_vars = ["DT_ATMOS", "LAYOUT_X", "LAYOUT_Y", "BLOCKSIZE"]
        for param, value in grid_params.items():
            if param in special_vars:
                param_val = fcst_config.get(param)
                if param_val and isinstance(param_val, str) and "{{" not in param_val:
                    continue
                elif isinstance(param_val, (int, float)):
                    continue
                # DT_ATMOS needs special treatment based on CCPP suite
                elif param == "DT_ATMOS":
                    if (
                        ccpp_suite in hires_ccpp_suites
                        and grid_params[param] > 40
                    ):
                        logger.warning(
                            dedent(
                                f"""
                            WARNING: CCPP suite {ccpp_suite} requires short
                            time step regardless of grid resolution; setting DT_ATMOS to 40.\n
                            This value can be overwritten in the user config file.
                            """
                            )
                        )
                        fcst_config[param] = 40
                    else:
                        fcst_config[param] = value
                else:
                    fcst_config[param] = value
            elif param.startswith("WRTCMP"):
                if fcst_config.get(param, "").strip("'") == "":
                    fcst_config[param] = value
            elif param == "GRID_GEN_METHOD":
                workflow_config[param] = value
            else:
                grid_config[param] = value

    # This logic belongs in predef_grid_params.yaml once merged with make_grid integration.
    if predef_grid_name == "RRFS_NA_3km":
        fv3_namelist = fcst_config["fv3"]["namelist"]
        fv3_namlelist["update_values"]["fms2_io_nml"][
            "netcdf_default_format"
        ] = "netcdf4"

    # Load model write component grid settings
    quilting_cfg = get_yaml_config(Path(USHdir, "quilting.yaml"))
    if not quilting:
        update_dict(quilting_cfg["no_quilting"], expt_config)
    else:
        write_grid = fcst_config["WRTCMP_output_grid"]
        update_dict(quilting_cfg[write_grid], expt_config)

    run_envir = expt_config["user"].get("RUN_ENVIR", "")

    fcst_len_hrs = workflow_config.get("FCST_LEN_HRS")
    date_first_cycl = workflow_config.get("DATE_FIRST_CYCL")
    date_last_cycl = workflow_config.get("DATE_LAST_CYCL")
    incr_cycl_freq = int(workflow_config.get("INCR_CYCL_FREQ"))

    # set varying forecast lengths only when fcst_len_hrs=-1
    if fcst_len_hrs == -1:
        fcst_len_cycl = workflow_config.get("FCST_LEN_CYCL")

        # Check that the number of entries divides into a day
        if 24 / incr_cycl_freq != len(fcst_len_cycl):
            # Also allow for the possibility that the user is running
            # cycles for less than a day:
            num_cycles = len(
                set_cycle_dates(date_first_cycl, date_last_cycl, incr_cycl_freq)
            )

            if num_cycles != len(fcst_len_cycl):
                logger.error(
                    f""" The number of entries in FCST_LEN_CYCL does
              not divide evenly into a 24 hour day or the number of cycles
              in your experiment! 
                FCST_LEN_CYCL = {fcst_len_cycl}
              """
                )
                raise ValueError

        # Build cycledef entries for the long forecasts
        # Short forecast cycles will be relevant to all intended
        # forecasts...after all, a 12 hour forecast also encompasses a 3
        # hour forecast, so the short ones will be consistent with the
        # existing default forecast cycledef

        # Reset the hours to the short forecast length
        workflow_config["FCST_LEN_HRS"] = min(fcst_len_cycl)

        # Find the entries that match the long forecast, and map them to
        # their time of day.
        long_fcst_len = max(fcst_len_cycl)
        long_indices = [i for i, x in enumerate(fcst_len_cycl) if x == long_fcst_len]
        long_cycles = [i * incr_cycl_freq for i in long_indices]

        # add one forecast entry per cycle per day
        fcst_cdef = []

        for hh in long_cycles:
            first = date_first_cycl.replace(hour=hh).strftime("%Y%m%d%H%S")
            last = date_last_cycl.replace(hour=hh).strftime("%Y%m%d%H%S")
            spec = f'{first} {last} 24:00:00'

            rocoto_config['cycledef'].append(
                {
                    "attrs": {"group": "long_forecast"},
                    "spec": spec
                 }
            )

    # check the availability of restart intervals for restart capability of forecast
    do_fcst_restart = fcst_config.get("DO_FCST_RESTART")
    if do_fcst_restart:
        restart_interval = fcst_config.get("RESTART_INTERVAL")
        restart_hrs = []
        if " " in str(restart_interval):
            restart_hrs = restart_interval.split()
        else:
            restart_hrs.append(str(restart_interval))

        lbc_spec_intvl_hrs = get_extrn_lbcs["LBC_SPEC_INTVL_HRS"]
        for irst in restart_hrs:
            rem_rst = int(irst) % lbc_spec_intvl_hrs
            if rem_rst != 0:
                raise Exception(
                    f"""
                The restart interval is not divided by LBC_SPEC_INTVL_HRS:
                  RESTART_INTERVAL = {irst}
                  LBC_SPEC_INTVL_HRS = {lbc_spec_intvl_hrs}"""
                )

    # if inline post is selected, add a upp block to task_run_fcst
    if fcst_config["fv3"]["model_configure"]["update_values"]["write_dopost"]:
        upp_config = copy.deepcopy(expt_config["task_run_post"]["upp"])
        upp_namelist_config = upp_config["namelist"]["update_values"]
        for key in ("datestr", "filename", "filenameflux", "grib", "ioform"):
            upp_namelist_config["model_inputs"].pop(key)
        upp_config["rundir"] = fcst_config["fv3"]["rundir"]
        fcst_config["upp"] = upp_config
    #
    # -----------------------------------------------------------------------
    #
    # Set parameters according to the type of horizontal grid generation
    # method specified.
    #
    # -----------------------------------------------------------------------
    #
    grid_gen_method = workflow_config["GRID_GEN_METHOD"]
    if grid_gen_method == "GFDLgrid":
        grid_params = set_gridparams_GFDLgrid(
            lon_of_t6_ctr=grid_config["GFDLgrid_LON_T6_CTR"],
            lat_of_t6_ctr=grid_config["GFDLgrid_LAT_T6_CTR"],
            res_of_t6g=grid_config["GFDLgrid_NUM_CELLS"],
            stretch_factor=grid_config["GFDLgrid_STRETCH_FAC"],
            refine_ratio_t6g_to_t7g=grid_config["GFDLgrid_REFINE_RATIO"],
            istart_of_t7_on_t6g=grid_config["GFDLgrid_ISTART_OF_RGNL_DOM_ON_T6G"],
            iend_of_t7_on_t6g=grid_config["GFDLgrid_IEND_OF_RGNL_DOM_ON_T6G"],
            jstart_of_t7_on_t6g=grid_config["GFDLgrid_JSTART_OF_RGNL_DOM_ON_T6G"],
            jend_of_t7_on_t6g=grid_config["GFDLgrid_JEND_OF_RGNL_DOM_ON_T6G"],
            verbose=verbose,
            nh4=expt_config["constants"]["NH4"],
            run_envir=run_envir,
        )
    elif grid_gen_method == "ESGgrid":
        grid_params = set_gridparams_ESGgrid(
            lon_ctr=grid_config["ESGgrid_LON_CTR"],
            lat_ctr=grid_config["ESGgrid_LAT_CTR"],
            nx=grid_config["ESGgrid_NX"],
            ny=grid_config["ESGgrid_NY"],
            pazi=grid_config["ESGgrid_PAZI"],
            halo_width=grid_config["ESGgrid_WIDE_HALO_WIDTH"],
            delx=grid_config["ESGgrid_DELX"],
            dely=grid_config["ESGgrid_DELY"],
            constants=expt_config["constants"],
        )
    else:

        errmsg = dedent(
            f"""
            Valid values of GRID_GEN_METHOD are GFDLgrid and ESGgrid.
            The value provided is:
              GRID_GEN_METHOD = {grid_gen_method}
            """
        )
        raise KeyError(errmsg) from None

    # Add a grid parameter section to the experiment config
    expt_config["grid_params"] = grid_params

    # Check to make sure that mandatory forecast variables are set.
    vlist = [
        "DT_ATMOS",
        "LAYOUT_X",
        "LAYOUT_Y",
        "BLOCKSIZE",
    ]
    for val in vlist:
        if not fcst_config.get(val):
            raise Exception(f"\nMandatory variable '{val}' has not been set\n")

    #
    # -----------------------------------------------------------------------
    #
    # Update the FV3 configuration, if needed
    #
    # -----------------------------------------------------------------------

    use_merra_climo = ccpp_suite in (
            "FV3_GFS_v15_thompson_mynn_lam3km",
            "FV3_GFS_v17_p8",
            )
    workflow_config["USE_MERRA_CLIMO"] = use_merra_climo
    # Add more fix files if MERRA2 files are needed
    if use_merra_climo:
        aero_files = {}
        fix_clim = Path(workflow_config["FIXclim"])
        fix_files = glob.glob("merra2.aerclim*.nc",
                root_dir=Path(platform_config["FIXaer"]))
        for file_path in fix_files:
            fp = Path(file_path)
            fn_month = fp.stem.split(".")[-1]
            aero_files[f"aeroclim.{fn_month}.nc"] = str(fix_clim / fp.name)

        fix_files = glob.glob("optics*.dat",
                root_dir=Path(platform_config["FIXlut"]))
        for file_path in fix_files:
            fp = Path(file_path)
            linkname = ".".join(fp.name.split(".")[0::2])
            aero_files[linkname] = str(fix_clim / fp.name)
        expt_config.update_from({"task_run_fcst": {"fv3": {"files_to_link": aero_files}}})


    # Check to make sure all SPP and LSM_SPP lists are the same length.
    global_sect = expt_config["global"]
    if global_sect.get("DO_SPP"):
        stoch_config = fcst_config["fv3"]["namelist"]["update_values"]["nam_sppperts"]
        list_vars = (
            "iseed_spp",
            "spp_lscale",
            "spp_prt_list",
            "spp_sigtop1",
            "spp_sigtop2",
            "spp_stddev_cutoff",
            "spp_tau",
            "spp_var_list",
        )
        list_len = fcst_config["fv3"]["namelist"]["update_values"]["n_var_spp"]
        if any([len(stoch_config[v]) != list_len for v in list_vars]):
            report = "\n".join([f"{v}: {len(stoch_config[v])}" for v in list_vars])
            raise Exception(
                f"""
                All MYNN PBL, MYNN SFC, GSL GWD, Thompson MP, or RRTMG SPP-related namelist
                variables must be of length equal to namelist setting
                "n_var_spp".
                  n_var_spp:  {list_len}

                Relevant namelist settings have counts:

                  {report}
                """
            )

    if global_sect.get("DO_LSM_SPP"):
        stoch_config = fcst_config["fv3"]["namelist"]["update_values"]["nam_sfcperts"]
        list_vars = ("lndp_tau", "lndp_lscale", "lndp_var_list", "lndp_prt_list")
        list_len = fcst_config["namelist"]["update_values"]["n_var_lndp"]
        if any([len(stoch_config[v]) != list_len for v in list_vars]):
            report = "\n".join([f"{v}: {len(stoch_config[v])}" for v in list_vars])
            raise Exception(
                f"""
                All Noah or RUC-LSM SPP-related namelist variables (except ISEED_LSM_SPP)
                must be equal of equal length to the n_var_lndp namelist
                setting:
                  n_var_lndp: {list_len}
                  {lsm_spp_var} (length {len(global_sect[lsm_spp_var])}

                Relevant namelist settings have counts:

                  {report}
                """
            )

    # Check whether the forecast length (FCST_LEN_HRS) is evenly divisible
    # by the BC update interval (LBC_SPEC_INTVL_HRS). If so, generate an
    # array of forecast hours at which the boundary values will be updated.

    lbc_spec_intvl_hrs = get_extrn_lbcs.get("LBC_SPEC_INTVL_HRS")
    rem = fcst_len_hrs % lbc_spec_intvl_hrs
    if rem != 0 and fcst_len_hrs > 0:
        raise Exception(
            f"""
            The forecast length (FCST_LEN_HRS) is not evenly divisible by the lateral
            boundary conditions update interval (LBC_SPEC_INTVL_HRS):
              FCST_LEN_HRS = {fcst_len_hrs}
              LBC_SPEC_INTVL_HRS = {lbc_spec_intvl_hrs}
              rem = FCST_LEN_HRS%%LBC_SPEC_INTVL_HRS = {rem}"""
        )

    #
    # -----------------------------------------------------------------------
    #
    # Post-processing validation and settings
    #
    # -----------------------------------------------------------------------
    #

    # If using external CRTM fix files to allow post-processing of synthetic
    # satellite products from the UPP, make sure the CRTM fix file directory exists.
    if global_sect.get("USE_CRTM"):
        crtm_dir = global_sect.get("CRTM_DIR")
        try:
            # os.path.exists returns exception if passed None, so use
            # "try/except" to catch it and the non-existence of a
            # provided path
            if not os.path.exists(crtm_dir):
                raise FileNotFoundError(
                    dedent(
                        f"""
                    USE_CRTM has been set, but the external CRTM fix file directory:
                    CRTM_DIR = {crtm_dir}
                    could not be found."""
                    )
                ) from None
        except TypeError:
            raise TypeError(
                dedent(
                    f"""
                USE_CRTM has been set, but the external CRTM fix file
                directory (CRTM_DIR) is None.
                """
                )
            ) from None
        except FileNotFoundError:
            raise

    post_config = expt_config["task_run_post"]

    # Make sure the post output domain is set
    post_output_domain_name = post_config["post_output_domain_name"]

    if "{{ " in post_output_domain_name:
        raise Exception(
            f"""

            The preferred output domain from the post task has not been set:

            task_run_post:
              post_output_domain_name: {post_output_domain_name}

            """
        )

    #
    # -----------------------------------------------------------------------
    #
    # Set the output directory locations
    #
    # -----------------------------------------------------------------------
    #
    # Use env variables for NCO variables and create NCO directories
    workflow_manager = platform_config.get("WORKFLOW_MANAGER")
    if run_envir == "nco" and workflow_manager == "rocoto":
        # Update the rocoto string for the fcst output location if
        # running an ensemble in nco mode
        if global_sect["DO_ENSEMBLE"]:
            rocoto_config["entities"][
                "FCST_DIR"
            ] = "{{ nco.PTMP }}/{{ nco.envir_default }}/tmp/run_fcst_mem#mem#.{{ workflow.WORKFLOW_ID }}_@Y@m@d@H"

    # create experiment dir
    mkdir_vrfy(f' -p "{exptdir}"')

    # -----------------------------------------------------------------------
    #
    # The FV3 forecast model needs the following input files in the run
    # directory to start a forecast:
    #
    #   (1) The data table file
    #   (2) The diagnostics table file
    #   (3) The field table file
    #   (4) The FV3 namelist file
    #   (5) The model configuration file
    #   (6) The NEMS configuration file
    #   (7) The CCPP physics suite definition file
    #
    # The workflow contains templates for the first six of these files.
    # Template files are versions of these files that contain placeholder
    # (i.e. dummy) values for various parameters.  The experiment/workflow
    # generation scripts copy these templates to appropriate locations in
    # the experiment directory (either the top of the experiment directory
    # or one of the cycle subdirectories) and replace the placeholders in
    # these copies by actual values specified in the experiment/workflow
    # configuration file (or derived from such values).  The scripts then
    # use the resulting "actual" files as inputs to the forecast model.
    #
    # Note that the CCPP physics suite definition file does not have a
    # corresponding template file because it does not contain any values
    # that need to be replaced according to the experiment/workflow
    # configuration.  If using CCPP, this file simply needs to be copied
    # over from its location in the forecast model's directory structure
    # to the experiment directory.
    #
    # Below, we first set the names of the templates for the first six files
    # listed above.  We then set the full paths to these template files.
    # Note that some of these file names depend on the physics suite while
    # others do not.
    #
    # -----------------------------------------------------------------------
    #
    # Check for the CCPP_PHYSICS suite xml file
    ccpp_phys_suite_in_ccpp_fp = workflow_config["CCPP_PHYS_SUITE_IN_CCPP_FP"]
    if not os.path.exists(ccpp_phys_suite_in_ccpp_fp):
        raise FileNotFoundError(
            f"""
            The CCPP suite definition file (CCPP_PHYS_SUITE_IN_CCPP_FP) does not exist
            in the local clone of the ufs-weather-model:
              CCPP_PHYS_SUITE_IN_CCPP_FP = '{ccpp_phys_suite_in_ccpp_fp}'"""
        )

    # Check for the field dict file
    field_dict_in_uwm_fp = workflow_config["FIELD_DICT_IN_UWM_FP"]
    if not os.path.exists(field_dict_in_uwm_fp):
        raise FileNotFoundError(
            f"""
            The field dictionary file (FIELD_DICT_IN_UWM_FP) does not exist
            in the local clone of the ufs-weather-model:
              FIELD_DICT_IN_UWM_FP = '{field_dict_in_uwm_fp}'"""
        )

    #
    # -----------------------------------------------------------------------
    #
    # Check that the set of tasks to run in the workflow is internally
    # consistent.
    #
    # -----------------------------------------------------------------------
    #
    # Get list of all top-level tasks and metatasks in the workflow.
    task_defs = rocoto_config.get("tasks")
    all_tasks = [task for task in task_defs]

    # Get list of all valid top-level tasks and metatasks pertaining to ensemble
    # verification.
    ens_vx_task_defns = load_config_file(
        os.path.join(USHdir, os.pardir, "parm", "wflow", "verify_ens.yaml")
    )
    ens_vx_valid_tasks = [task for task in ens_vx_task_defns]

    # Get list of all valid top-level tasks and metatasks in the workflow that
    # pertain to ensemble verification.
    ens_vx_tasks = [task for task in ens_vx_valid_tasks if task in all_tasks]

    # Get the value of the configuration flag for ensemble mode (DO_ENSEMBLE)
    # and ensure that it is set to True if ensemble vx tasks are included in
    # the workflow (or vice-versa).
    do_ensemble = global_sect["DO_ENSEMBLE"]
    if (not do_ensemble) and ens_vx_tasks:
        task_str = "    " + "\n    ".join(ens_vx_tasks)
        msg = dedent(
            f"""
              Ensemble verification can not be run unless running in ensemble mode:
                  DO_ENSEMBLE = \"{do_ensemble}\"
              Ensemble verification tasks:
                  {task_str}
              Please set DO_ENSEMBLE to True or remove ensemble vx tasks from the
              workflow."""
        )
        raise Exception(msg)

    #
    # -----------------------------------------------------------------------
    # NOTE: currently this is executed no matter what, should it be dependent on the logic described below??
    # If not running the TN_MAKE_GRID, TN_MAKE_OROG, and/or TN_MAKE_SFC_CLIMO
    # tasks, create symlinks under the FIXlam directory to pregenerated grid,
    # orography, and surface climatology files.
    #
    # -----------------------------------------------------------------------
    #
    fixlam = workflow_config["FIXlam"]
    mkdir_vrfy(f' -p "{fixlam}"')

    #
    # Use the pregenerated domain files if the tasks to generate them
    # are turned off. Link the files, and check that they all contain
    # the same resolution input.
    #

    def _dict_find(user_dict, substring):

        if not isinstance(user_dict, dict):
            return False

        for key, value in user_dict.items():
            if substring in key:
                return True
            if isinstance(value, dict):
                if _dict_find(value, substring):
                    return True

        return False

    run_make_ics = _dict_find(rocoto_tasks, "task_make_ics")
    run_make_lbcs = _dict_find(rocoto_tasks, "task_make_lbcs")
    run_run_fcst = _dict_find(rocoto_tasks, "task_run_fcst")
    run_any_coldstart_task = run_make_ics or \
                             run_make_lbcs or \
                             run_run_fcst
    # Flags for creating symlinks to pre-generated grid, orography, and sfc_climo files.
    # These consider dependencies of other tasks on each pre-processing task.
    create_symlinks_to_pregen_files = {
        "GRID": (not run_make_grid)
        and (run_make_orog or run_make_sfc_climo or run_any_coldstart_task),
        "OROG": (not run_make_orog) and (run_make_sfc_climo or run_any_coldstart_task),
        "SFC_CLIMO": (not run_make_sfc_climo) and (run_make_ics or run_make_lbcs),
    }

    fixed_files = expt_config["fixed_files"]

    prep_tasks = ["GRID", "OROG", "SFC_CLIMO"]
    res_in_fixlam_filenames = None
    for prep_task in prep_tasks:
        res_in_fns = ""
        sect_key = f"task_make_{prep_task.lower()}"
        # If the user doesn't want to run the given task, link the fix
        # file from the staged files.
        if not task_defs.get(sect_key):
            dir_key = f"{prep_task}_DIR"
            task_dir = expt_config[sect_key].get(dir_key)

            if not task_dir:
                task_dir = os.path.join(pregen_basedir, predef_grid_name)
                expt_config[sect_key][dir_key] = task_dir
                msg = dedent(
                    f"""
                   {dir_key} will point to a location containing pre-generated files.
                   Setting {dir_key} = {task_dir}
                   """
                )
                logger.warning(msg)

            if not os.path.exists(task_dir):
                msg = dedent(
                    f"""
                    File directory does not exist!
                    {dir_key} needs {task_dir}
                    """
                )
                raise FileNotFoundError(msg)

            # Link the fix files and check that their resolution is
            # consistent
            res_in_fns = link_fix(
                verbose=verbose,
                file_group=prep_task.lower(),
                source_dir=task_dir,
                target_dir=workflow_config["FIXlam"],
                ccpp_phys_suite=ccpp_suite,
                constants=expt_config["constants"],
                dot_or_uscore=workflow_config["DOT_OR_USCORE"],
                nhw=grid_params["NHW"],
                run_task=False,
                sfc_climo_fields=fixed_files["SFC_CLIMO_FIELDS"],
            )
            if not res_in_fixlam_filenames:
                res_in_fixlam_filenames = res_in_fns
            else:
                if res_in_fixlam_filenames != res_in_fns:
                    raise Exception(
                        dedent(
                            f"""
                        The resolution of the pregenerated files for
                        {prep_task} do not match those that were alread
                        set:

                        Resolution in {prep_task}: {res_in_fns}
                        Resolution expected: {res_in_fixlam_filenames}
                        """
                        )
                    )

            if not os.path.exists(task_dir):
                raise FileNotFoundError(
                    f'''
                    The directory ({dir_key}) that should contain the pregenerated
                    {prep_task.lower()} files does not exist:
                      {dir_key} = \"{task_dir}\"'''
                )

    workflow_config["RES_IN_FIXLAM_FILENAMES"] = res_in_fixlam_filenames
    if res_in_fixlam_filenames:
        workflow_config["CRES"] = f"C{res_in_fixlam_filenames}"
    elif cres := os.getenv("CRES"):
        workflow_config["CRES"] = cres






    #
    # -----------------------------------------------------------------------
    #
    # Turn off post task if it's not consistent with the forecast's
    # user-setting of WRITE_DOPOST
    #
    # -----------------------------------------------------------------------
    #
    if fcst_config["fv3"]["model_configure"]["update_values"]["write_dopost"]:
        # Turn off run_post
        task_name = "metatask_run_ens_post"
        removed_task = task_defs.pop(task_name, None)
        if removed_task:
            logger.warning(
                dedent(
                    f"""
                     Inline post is turned on, deactivating post-processing tasks:
                     Removing {task_name} from task definitions
                     list.
                     """
                )
            )

    #
    # -----------------------------------------------------------------------
    #
    # Read CCPP suite definition file and perform actions based on its
    # contents as necessary
    #
    # -----------------------------------------------------------------------
    #

    ccpp_suite_xml = load_xml_file(workflow_config["CCPP_PHYS_SUITE_IN_CCPP_FP"])

    # Need to track if we are using RUC LSM for the make_ics step
    workflow_config["SDF_USES_RUC_LSM"] = has_tag_with_value(
        ccpp_suite_xml, "scheme", "lsm_ruc"
    )

    # Thompson microphysics needs additional input files and namelist settings
    workflow_config["SDF_USES_THOMPSON_MP"] = has_tag_with_value(
        ccpp_suite_xml, "scheme", "mp_thompson"
    )

    if workflow_config["SDF_USES_THOMPSON_MP"]:
        logging.debug(
            f'Selected CCPP suite ({ccpp_suite}) uses Thompson MP'
        )
        logging.debug(f"Setting up links for additional fix files")

        # If the model ICs or BCs are not from RAP or HRRR, they will not contain aerosol
        # climatology data needed by the Thompson scheme, so we need to provide a separate file
        # Add thompson-specific fix files to the FV3 configuration
        thompson_files = fixed_files["THOMPSON_FIX_FILES"]
        if get_extrn_ics["EXTRN_MDL_NAME_ICS"] not in ["HRRR", "RRFS", "RAP"] or get_extrn_lbcs[
            "EXTRN_MDL_NAME_LBCS"
        ] not in ["HRRR", "RRFS", "RAP"]:
            thompson_files.append(workflow_config["THOMPSON_MP_CLIMO_FN"])

        # Add thompson-specific fix files to the FV3 configuration
        fixam = workflow_config["FIXam"]
        thompson_fix_links = {fn: f"{fixam}/{fn}" for fn in thompson_files}
        fcst_config["fv3"]["files_to_link"].update(thompson_fix_links)

    #
    # -----------------------------------------------------------------------
    #
    # Generate var_defns.sh file in the EXPTDIR. This file contains all
    # the user-specified settings from expt_config.
    #
    # -----------------------------------------------------------------------
    #

    expt_config.dereference()
    for sect, sect_keys in expt_config.items():
        for k, v in sect_keys.items():
            expt_config[sect][k] = str_to_list(v)
    expt_config.dereference()

    # print content of var_defns if DEBUG=True
    all_lines = cfg_to_yaml_str(expt_config)
    log_info(all_lines, verbose=debug)


    # Write out a base namelist for the physics suite
    physics_base_nml = get_nml_config(expt_config.pop("fv3_namelist_settings"))
    base_nml_path = Path(fcst_config["fv3"]["namelist"]["base_file"])
    physics_base_nml.dump(base_nml_path)

    global_var_defns_fp = workflow_config["GLOBAL_VAR_DEFNS_FP"]
    # print info message
    log_info(
        f"""
        Generating the global experiment variable definitions file here:
          GLOBAL_VAR_DEFNS_FP = '{global_var_defns_fp}'
        For more detailed information, set DEBUG to 'TRUE' in the experiment
        configuration file ('{user_config_fn}')."""
    )

    # Final failsafe before writing rocoto yaml to ensure we don't have any invalid dicts
    # (e.g. metatasks with no tasks, tasks with no associated commands)
    clean_rocoto_dict(expt_config["rocoto"]["tasks"])

    rocoto_yaml_fp = Path(workflow_config["ROCOTO_YAML_FP"])
    rocoto_yaml = get_yaml_config({"workflow": expt_config["rocoto"]})
    rocoto_yaml.dump(rocoto_yaml_fp)

    var_defns_cfg = get_yaml_config(config=expt_config.data)
    del var_defns_cfg["rocoto"]

    # Fixup a couple of data types:
    for dates in ("DATE_FIRST_CYCL", "DATE_LAST_CYCL"):
        var_defns_cfg["workflow"][dates] = date_to_str(var_defns_cfg["workflow"][dates])
    var_defns_cfg.dump(Path(global_var_defns_fp))

    #
    # -----------------------------------------------------------------------
    #
    # Check validity of parameters in one place, here in the end.
    #
    # -----------------------------------------------------------------------
    #
    # Validate experiment config against schema
    schema = Path(USHdir) / "experiment.jsonschema"
    valid = validate(schema_file=schema, config=var_defns_cfg)

    if not valid:
        logging.error(f"Experiment configuration is not valid against schema")
        sys.exit(1)

    return expt_config


def clean_rocoto_dict(rocotodict):
    """Removes any invalid entries from ``rocotodict``. Examples of invalid entries are:

    1. A task dictionary containing no "command" key
    2. A metatask dictionary containing no task dictionaries
    
    Args: 
        rocotodict (dict): A dictionary containing Rocoto workflow settings
    """

    # Loop 1: search for tasks with no command key, iterating over metatasks
    for key in list(rocotodict.keys()):
        if key.split("_", maxsplit=1)[0] == "metatask":
            clean_rocoto_dict(rocotodict[key])
        elif key.split("_", maxsplit=1)[0] in ["task"]:
            if not rocotodict[key].get("command"):
                popped = rocotodict.pop(key)
                logging.warning(
                    f"Invalid task {key} removed due to empty/unset run command"
                )
                logging.debug(f"Removed entry:\n{popped}")

    # Loop 2: search for metatasks with no tasks in them
    for key in list(rocotodict.keys()):
        if key.split("_", maxsplit=1)[0] == "metatask":
            valid = False
            for key2 in list(rocotodict[key].keys()):
                if key2.split("_", maxsplit=1)[0] == "metatask":
                    clean_rocoto_dict(rocotodict[key][key2])
                    # After above recursion, any nested empty metatasks will have popped themselves
                    if rocotodict[key].get(key2):
                        valid = True
                elif key2.split("_", maxsplit=1)[0] == "task":
                    valid = True
            if not valid:
                popped = rocotodict.pop(key)
                logging.warning(f"Invalid/empty metatask {key} removed")
                logging.debug(f"Removed entry:\n{popped}")


#
# -----------------------------------------------------------------------
#
# Call the function defined above.
#
# -----------------------------------------------------------------------
#
if __name__ == "__main__":
    USHdir = os.path.dirname(os.path.abspath(__file__))
    setup(USHdir)
