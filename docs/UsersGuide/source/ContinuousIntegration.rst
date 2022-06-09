.. _ContinuousIntegration:

======================
Continuous Integration
======================
The Continuous Integration pipeline uses `GitHub Actions Self-Hosted Runners <https://docs.github.com/en/actions/hosting-your-own-runners/about-self-hosted-runners>`_. 
Currently, an `AWS Parallel Cluster <https://aws.amazon.com/hpc/parallelcluster>`_ is configured with all of the dependencies required of the ufs-srweather-app, emulating a NOAA RDHPCS machine.  


The following dependencies are currently requirements on the self-hosted runner to build and run automated workflows:

* Python v3.8
* Ruby
* IntelOne API
* Lua
* Lmod
* `hpc-stack <https://github.com/NOAA-EMC/hpc-stack>`_ with all of necessary libraries in ``ufs-srweather-app/env/srw_common``
* Rocoto
* Miniconda3

After creating an AWS ParallelCluster, a ``post-install`` `script <https://github.com/robgonzalezpita/rrfs-ci-pcluster/blob/main/rrfs_ci_post_install.sh>`_
can be executed to automate the installation of these dependencies. 

One must then configure this ParallelCluster to be a self-hosted runner as well (which can be done via the `GitHub interface <https://docs.github.com/en/actions/hosting-your-own-runners/adding-self-hosted-runners>`_), in order to pick up the jobs listed in
the GitHub Actions workflow files. 

As of now, only a build test has been implemented to verify that the ``ufs-srweather-app`` builds on the self-hosted runner. A test suite consisting of Workflow End to End tests will be added at a later date.

