from __future__ import absolute_import
from __future__ import print_function  # so print doesn't show brackets

import math
import matplotlib.pyplot as plt
import matplotlib
import json
import numpy as np
import itertools as itr
import os as os
import sys as sys
import pandas as pd
import warnings
import time as time
from time import sleep
import random

import pickle
import qinfer
import redis

# QMLA functionality
from qmla.remote_bayes_factor import *
import qmla.analysis
import qmla.database_framework as database_framework
import qmla.database_launch as database_launch
import qmla.get_growth_rule as get_growth_rule
import qmla.expectation_values as expectation_values 
from qmla.remote_model_learning import *
import qmla.model_naming as model_naming
import qmla.model_generation as model_generation
import qmla.model_instances as QML
import qmla.redis_settings as rds

pickle.HIGHEST_PROTOCOL = 2  # TODO if >python3, can use higher protocol
plt.switch_backend('agg')

__all__ = [
    'QuantumModelLearningAgent'
]

def time_seconds():
    import datetime
    now = datetime.date.today()
    hour = datetime.datetime.now().hour
    minute = datetime.datetime.now().minute
    second = datetime.datetime.now().second
    time = str(str(hour) + ':' + str(minute) + ':' + str(second))
    return time


class QuantumModelLearningAgent():
    """
    - This class manages quantum model development.
    - This is done by controlling a pandas database,
        sending model specifications
        to remote actors (via RQ) to compute QHL,
        and also Bayes factors, generating
        a next set of models iteratively.
    - This is done in a tree like growth mechanism where
        new branches consist of
        models generated considering previously determined "good" models.
    - Model generation rules are given in model_generation.
    - Database control is given in database_framework.
    - Remote functions for computing QHL/Bayes factors are in
    - remote_model_learning and remote_bayes_factor respectively.
    - Redis databases are used to ensure QMD parameters are accessible to
        remote models (since shared memory is not available).
        Relevant QMD parameters and info are pickled to redis.

    """

    def __init__(self,
                 global_variables, # TODO make default global variables class available 
                 generator_list=[],
                 first_layer_models=['x'],
                 probe_dict=None,
                 sim_probe_dict=None,
                 model_priors=None, # needed for further QHL mode
                 experimental_measurements=None, # TODO get exp measurements from global variables
                 results_directory='',
                 use_exp_custom=True, # TODO either remove custom exponentiation method or fix
                 plot_times=[0, 1],
                 sigma_threshold=1e-13,
                 **kwargs
                 ):
        self._start_time = time.time() # to measure run-time

        # Configure this QMLA instance
        self.qmla_controls = global_variables
        self.growth_class = self.qmla_controls.growth_class

        # Basic settings, path definitions etc
        self._fundamental_settings()

        # Info on true model
        self._true_model_definition()

        # Parameters related to learning/comparing models
        self._set_learning_and_comparison_parameters(
            model_priors = model_priors, 
            system_probe_dict = probe_dict,
            simulation_probe_dict = sim_probe_dict,
            experimental_measurements = experimental_measurements, 
            plot_times = plot_times
        )

        # Redundant terms -- TODO remove calls to them and then attributes
        self._potentially_redundant_setup(
            first_layer_models = first_layer_models, 
            use_exp_custom = use_exp_custom, 
            sigma_threshold = sigma_threshold, 
        )

        # set up all attributes related to growth rules and tree management
        self._setup_tree_and_growth_rules(            
            generator_list = generator_list, 
        )

        # check if QMLA should run in parallel and set up accordingly
        self._setup_parallel_requirements()

        # resources potentiall reallocated based on number of parameters/dimension
        self._compute_base_resources()
        
        # QMLA core info stored on redis server
        self._compile_and_store_qmla_info_summary()
        
        # Database used to keep track of models tested
        self._initiate_database()


    ##########
    # Section: Initialisation
    ##########

    def _fundamental_settings(self):
        self.qmla_id = self.qmla_controls.qmd_id
        self.use_experimental_data = self.qmla_controls.use_experimental_data
        self.redis_host_name = self.qmla_controls.host_name
        self.redis_port_number = self.qmla_controls.port_number
        self.log_file = self.qmla_controls.log_file        
        self.qhl_mode = self.qmla_controls.qhl_test
        self.qhl_mode_multiple_models = self.qmla_controls.multiQHL
        self.results_directory = self.qmla_controls.results_directory
        if not self.results_directory.endswith('/'):
            self.results_directory += '/'
        self.latex_name_map_file_path = self.qmla_controls.latex_mapping_file
        self.log_print(["Retrieving databases from redis"])
        self.redis_databases = rds.databases_from_qmd_id(
            self.redis_host_name,
            self.redis_port_number,
            self.qmla_id,
            # tree_identifiers=self.tree_identifiers
        )
        self.redis_databases['any_job_failed'].set('Status', 0)

    def _true_model_definition(self):
        self.true_model_name = self.qmla_controls.true_op_name
        self.true_model_dimension = database_framework.get_num_qubits(self.true_model_name)
        self.true_model_constituent_operators = self.qmla_controls.true_op_list
        self.true_model_num_params = self.qmla_controls.true_operator_class.num_constituents
        self.true_param_list = self.qmla_controls.true_params_list
        self.true_param_dict = self.qmla_controls.true_params_dict
        self.log_print(
            [
                "True model:", self.true_model_name
            ]
        )

    def _setup_tree_and_growth_rules(
        self,
        generator_list, 
    ):
        # Models and Bayes factors lists
        self.all_bayes_factors = {}
        self.bayes_factor_pair_computed = []
        self.model_name_id_map = {}
        self.HighestModelID = 0  # so first created model gets modelID=0

        # Growth rule setup
        self.growth_rules_list = generator_list
        self.growth_rules_initial_models = {}
        self.growth_rule_of_true_model = self.qmla_controls.growth_generation_rule
        zeroth_gen = self.growth_rules_list[0]
        matching_gen_idx = self.growth_rules_list.index(self.growth_rule_of_true_model)
        if self.growth_rules_list[0] != self.growth_rule_of_true_model:
            self.growth_rules_list[0] = self.growth_rule_of_true_model
            self.growth_rules_list[matching_gen_idx] = zeroth_gen
        self.UniqueGrowthClasses = {
            self.growth_rule_of_true_model: self.growth_class
        }  # to save making many instances
        self.spawn_depthByGrowthRule = {}

        # Tree/growth management 
        self.spawn_depth = 0
        self.tree_identifiers = [self.growth_rule_of_true_model]
        self.SpawnStage = {}
        self.MiscellaneousGrowthInfo = {}
        
        ## branch management
        self.branch_bayes_points = {}
        self.branch_rankings = {}
        self.branch_parents = {}
        self.BranchChampions = {}
        self.ActiveBranchChampList = []
        self.HighestBranchID = 0
        self.NumModelsPerBranch = {}
        self.NumModelPairsPerBranch = {}
        self.BranchAllModelsLearned = {}
        self.BranchComparisonsComplete = {}
        self.BranchNumModelsPreComputed = {}
        self.BranchBayesComputed = {}
        self.BranchModels = {}
        self.ModelsBranches = {}
        self.BranchPrecomputedModels = {}
        self.BranchModelIds = {}
        self.Branchget_growth_rule = {}
        self.BranchGrowthClasses = {}
        self.TreesCompleted = {}
        self.InitialOpsAllBranches = []
        self.InitialModelBranches = {}
        self.InitialModelIDs = {}
        self.BranchChampsByNumQubits = {}
        self.ghost_branch_list = []
        self.GhostBranches = {}

        self._setup_all_growth_rules()


    def _setup_all_growth_rules(self):
        initial_id_counter = 0
        models_already_added_to_a_branch = []
        for i in range(len(self.growth_rules_list)):
            # TODO remove use of self.InitialModList -- first layer models got here
            # to match this newly created branch with corresponding dicts
            # filled here
            gen = self.growth_rules_list[i]
            growth_class_gen = get_growth_rule.get_growth_generator_class(
                growth_generation_rule=gen,
                use_experimental_data=self.use_experimental_data,
                log_file=self.log_file
            )
            # self.TreesCompleted[gen] = False
            self.TreesCompleted[gen] = growth_class_gen.tree_completed_initially
            self.growth_rules_initial_models[gen] = growth_class_gen.initial_models

            self.BranchChampsByNumQubits[gen] = {}
            initial_models_this_gen = self.growth_rules_initial_models[gen]
            self.log_print(
                [
                    "initialising generator {} with models: {}".format(
                        gen,
                        initial_models_this_gen)
                ]
            )
            self.InitialOpsAllBranches.extend(initial_models_this_gen)
            num_new_models = len(initial_models_this_gen)
            self.BranchModelIds[i] = []
            self.BranchModels[i] = []
            self.BranchPrecomputedModels[i] = []
            self.BranchNumModelsPreComputed[i] = 0

            for mod in initial_models_this_gen:
                mod = database_framework.alph(mod)
                self.BranchModels[i].append(mod)
                # latest branch to claim it
                self.ModelsBranches[initial_id_counter] = i
                self.HighestBranchID = i
                # self.BranchModelIds[i].append(initial_id_counter)
                if mod in models_already_added_to_a_branch:
                    orig_mod_id = self.InitialModelIDs[mod]
                    self.log_print(
                        [
                            mod,
                            "already added as",
                            orig_mod_id
                        ]
                    )
                    self.BranchModelIds[i].append(orig_mod_id)
                    self.BranchPrecomputedModels[i].append(mod)
                    self.BranchNumModelsPreComputed[i] += 1
                else:
                    self.log_print(
                        [
                            mod,
                            "not added yet. List:",
                            models_already_added_to_a_branch
                        ]
                    )
                    self.BranchModelIds[i].append(initial_id_counter)
                    self.InitialModelIDs[mod] = initial_id_counter
                    self.InitialModelBranches[mod] = i
                    models_already_added_to_a_branch.append(mod)
                    initial_id_counter += 1

            # self.HighestModelID += num_new_models
            self.BranchBayesComputed[i] = False
            self.BranchAllModelsLearned[i] = False
            self.BranchComparisonsComplete[i] = False

            self.NumModelsPerBranch[i] = (
                len(self.growth_rules_initial_models[gen])
            )
            self.NumModelPairsPerBranch[i] = (
                num_pairs_in_list(len(
                    self.growth_rules_initial_models[gen])
                )
            )
            self.spawn_depthByGrowthRule[gen] = 0
            self.SpawnStage[gen] = [None]
            self.MiscellaneousGrowthInfo[gen] = {}
            self.Branchget_growth_rule[i] = gen
            # self.BranchGrowthClasses[i] = growth_class_gen
            if gen not in list(self.UniqueGrowthClasses.keys()):
                self.UniqueGrowthClasses[gen] = growth_class_gen
            self.BranchGrowthClasses[i] = self.UniqueGrowthClasses[gen]

        # self.HighestBranchID = max(self.InitialModelBranches.values())
        self.log_print(
            [
                "After initial branches. InitialModelIDs:",
                self.InitialModelIDs
            ]
        )
        # to ensure everywhere we use range(qmd.HighestModelID) goes to the
        # right number
        self.HighestModelID = max(self.InitialModelIDs.values()) + 1
        self.NumModels = len(self.InitialModelIDs.keys())
        self.log_print(
            [
                "After setting up initial branches, highest branch id:",
                self.HighestBranchID,
                "highest model id:", self.HighestModelID,
                "initial models:", self.model_name_id_map
            ]
        )

        # i.e. Trees only stem from unique generators
        self.NumTrees = len(self.growth_rules_list)
        # print("[QMD] num trees:", self.NumTrees)
        self.NumTreesCompleted = np.sum(
            list(self.TreesCompleted.values())
        )


    def _set_learning_and_comparison_parameters(
        self,
        model_priors, 
        system_probe_dict,
        simulation_probe_dict,
        experimental_measurements,
        plot_times
    ):
        self.ModelPriors = model_priors
        self.NumParticles = self.qmla_controls.num_particles
        self.NumExperiments = self.qmla_controls.num_experiments
        self.NumTimesForBayesUpdates = self.qmla_controls.num_times_bayes
        self.BayesLower = self.qmla_controls.bayes_lower
        self.BayesUpper = self.qmla_controls.bayes_upper
        self.ResampleThreshold = self.qmla_controls.resample_threshold
        self.ResamplerA = self.qmla_controls.resample_a
        self.PGHPrefactor = self.qmla_controls.pgh_factor
        self.PGHExponent = self.qmla_controls.pgh_exponent
        self.ReallocateResources = self.qmla_controls.reallocate_resources
        self.gaussian = self.qmla_controls.gaussian # TODO remove?
        if system_probe_dict is None:
            # ensure there is a probe set
            self.log_print(
                [
                    "Generating probes within QMLA"
                ]
            )
            self.growth_class.generate_probes(
                experimental_data=self.qmla_controls.use_experimental_data,
                noise_level=self.qmla_controls.probe_noise_level,
                minimum_tolerable_noise=0.0,
            )
            self.ProbeDict = self.growth_class.system_probes
            self.SimProbeDict = self.ProbeDict
        else:
            self.NumProbes = self.qmla_controls.num_probes
            self.log_print(
                [
                    "Probe dict provided to QMLA."
                ]
            )
            self.ProbeDict = system_probe_dict
            self.SimProbeDict = simulation_probe_dict
        
        self.ExperimentalMeasurements = experimental_measurements
        if self.ExperimentalMeasurements is not None:
            self.ExperimentalMeasurementTimes = (
                sorted(list(self.ExperimentalMeasurements.keys()))
            )
        else:
            self.ExperimentalMeasurementTimes = None
        self.PlotProbeFile = self.qmla_controls.plot_probe_file

        self.PlotTimes = plot_times
        self.ReducedPlotTimes = self.PlotTimes[0::10]
        self.PlotProbes = pickle.load(
            open(self.PlotProbeFile, 'rb')
        )
        if self.use_experimental_data == False:
            # TODO is this doing anything useful?
            # at least put in separate method
            self.ExperimentalMeasurements = {}
            self.TrueHamiltonian = self.qmla_controls.true_hamiltonian
            self.TrueHamiltonianDimension = np.log2(
                self.TrueHamiltonian.shape[0]
            )
            self.log_print(
                [
                    "Getting expectation values for simulated model",
                    "(len {})".format(len(self.PlotTimes)),
                    "\n Times computed:\n", self.PlotTimes
                ]
            )

            for t in self.PlotTimes:
                # TODO is this the right expectation value func???

                self.ExperimentalMeasurements[t] = (
                    self.growth_class.expectation_value(
                        ham=self.TrueHamiltonian,
                        t=t,
                        state=self.PlotProbes[self.true_model_dimension],
                        log_file=self.log_file,
                        log_identifier='[QMD Init]'
                    )

                )
            self.log_print(
                [
                    "Expectation values computed",
                ]
            )


    def _potentially_redundant_setup(
        self,
        first_layer_models,
        use_exp_custom, 
        sigma_threshold, 
    ):
        # testing whether these are used anywhere
        # Either remove, or find appropriate initialisation
        self.InitialOpList = first_layer_models
        self.QLE = False  # Set to False for IQLE # TODO remove - redundant
        self.MeasurementType = self.qmla_controls.measurement_type 
        self.UseExpCustom = use_exp_custom
        self.EnableSparse = True # should only matter when using custom exponentiation package
        self.ExpComparisonTol = None
        self.SigmaThreshold = sigma_threshold
        self.FitnessParameters = {}
        self.DebugDirectory = None
        self.NumTimeDepTrueParams = 0
        self.TimeDepParams = None
        self.UseTimeDepTrueModel = False
        self.BayesFactorsFolder = str(
            self.results_directory
            + 'BayesFactorsTimeRecords/'
        )
        if not os.path.exists(self.BayesFactorsFolder):
            try:
                os.makedirs(self.BayesFactorsFolder)
            except BaseException:
                # reached at exact same time as another process; don't crash
                pass
        self.BayesFactorsTimeFile = str(
            self.BayesFactorsFolder
            + 'BayesFactorsPairsTimes_'
            + str(self.qmla_controls.long_id)
            + '.txt'
        )
    def _setup_parallel_requirements(self):
        self.use_rq = self.qmla_controls.use_rq
        self.rq_timeout = self.qmla_controls.rq_timeout
        self.rq_log_file = self.log_file
        self.write_log_file = open(self.log_file, 'a')

        try:
            from rq import Connection, Queue, Worker
            self.redis_conn = redis.Redis(
                host=self.redis_host_name, port=self.redis_port_number)
            test_workers = self.use_rq
            self.rq_queue = Queue(
                self.qmla_id,
                connection=self.redis_conn,
                async=test_workers,
                default_timeout=self.rq_timeout
            )  # TODO is this timeout sufficient for ALL QMD jobs?

            parallel_enabled = True
        except BaseException:
            print("importing rq failed")
            parallel_enabled = False

        self.RunParallel = parallel_enabled

    def _compute_base_resources(self):
        # TODO remove base_num_qubits stuff?
        base_num_qubits = 3
        base_num_terms = 3
        for op in self.InitialOpList:
            if database_framework.get_num_qubits(op) < base_num_qubits:
                base_num_qubits = database_framework.get_num_qubits(op)
            num_terms = len(database_framework.get_constituent_names_from_name(op))
            if (
                num_terms < base_num_terms
            ):
                base_num_terms = num_terms

        self.BaseResources = {
            'num_qubits': base_num_qubits,
            'num_terms': base_num_terms,
        }


    def _compile_and_store_qmla_info_summary(
        self
    ):
        num_exp_ham = (
            self.NumParticles *
            (self.NumExperiments + self.NumTimesForBayesUpdates)
        )
        latex_config = str(
            '$P_{' + str(self.NumParticles) +
            '}E_{' + str(self.NumExperiments) +
            '}B_{' + str(self.NumTimesForBayesUpdates) +
            '}RT_{' + str(self.ResampleThreshold) +
            '}RA_{' + str(self.ResamplerA) +
            '}RP_{' + str(self.PGHPrefactor) +
            '}H_{' + str(num_exp_ham) +
            r'}|\psi>_{' + str(self.NumProbes) +
            '}PN_{' + str(self.qmla_controls.probe_noise_level) +
            '}BF^{bin }_{' + str(self.qmla_controls.bayes_time_binning) +
            '}BF^{all }_{' + str(self.qmla_controls.bayes_factors_use_all_exp_times) +
            '}$'
        )
        self.LatexConfig = latex_config
        print("[QMD] latex config:", self.LatexConfig)

        self.QMDInfo = {
            # may need to take copies of these in case pointers accross nodes
            # break
            'num_probes': self.NumProbes,
            #          'probe_dict' : self.ProbeDict, # possibly include here?
            'plot_probe_file': self.PlotProbeFile,
            'plot_times': self.PlotTimes,
            'true_oplist': self.true_model_constituent_operators,
            'true_params': self.true_param_list,
            'num_particles': self.NumParticles,
            'num_experiments': self.NumExperiments,
            'resampler_thresh': self.ResampleThreshold,
            'resampler_a': self.ResamplerA,
            'pgh_prefactor': self.PGHPrefactor,
            'pgh_exponent': self.PGHExponent,
            'increase_pgh_time': self.qmla_controls.increase_pgh_time,
            'store_particles_weights': False,
            'growth_generator': self.growth_rule_of_true_model,
            'qhl_plots': False, # can be used during dev
            'results_directory': self.results_directory,
            'plots_directory': self.qmla_controls.plots_directory,
            'long_id': self.qmla_controls.long_id,
            'debug_directory': self.DebugDirectory,
            'qle': self.QLE,
            'sigma_threshold': self.SigmaThreshold,
            'true_name': self.true_model_name,
            'use_exp_custom': self.UseExpCustom,
            'measurement_type': self.MeasurementType,
            'use_experimental_data': self.use_experimental_data,
            'experimental_measurements': self.ExperimentalMeasurements,
            'experimental_measurement_times': self.ExperimentalMeasurementTimes,
            'compare_linalg_exp_tol': self.ExpComparisonTol,
            'gaussian': self.gaussian,
            # 'bayes_factors_time_binning' : self.BayesTimeBinning,
            'bayes_factors_time_binning': self.qmla_controls.bayes_time_binning,
            'q_id': self.qmla_id,
            'use_time_dep_true_params': False,
            'time_dep_true_params': self.TimeDepParams,
            'num_time_dependent_true_params': self.NumTimeDepTrueParams,
            'prior_pickle_file': self.qmla_controls.prior_pickle_file,
            'prior_specific_terms': self.growth_class.gaussian_prior_means_and_widths,
            'model_priors': self.ModelPriors,
            'base_resources': self.BaseResources,
            'reallocate_resources': self.ReallocateResources,
            'param_min': self.qmla_controls.param_min,
            'param_max': self.qmla_controls.param_max,
            'param_mean': self.qmla_controls.param_mean,
            'param_sigma': self.qmla_controls.param_sigma,
            'tree_identifiers': self.tree_identifiers,
            'bayes_factors_time_all_exp_times': self.qmla_controls.bayes_factors_use_all_exp_times,
        }
        compressed_qmd_info = pickle.dumps(self.QMDInfo, protocol=2)
        compressed_probe_dict = pickle.dumps(self.ProbeDict, protocol=2)
        compressed_sim_probe_dict = pickle.dumps(self.SimProbeDict, protocol=2)
        qmd_info_db = self.redis_databases['qmd_info_db']
        self.log_print(["Saving qmd info db to ", qmd_info_db])
        qmd_info_db.set('QMDInfo', compressed_qmd_info)
        qmd_info_db.set('ProbeDict', compressed_probe_dict)
        qmd_info_db.set('SimProbeDict', compressed_sim_probe_dict)

    def log_print(self, to_print_list):
        identifier = str(str(time_seconds()) + " [QMD " + str(self.qmla_id) + "]")
        if not isinstance(to_print_list, list):
            to_print_list = list(to_print_list)

        print_strings = [str(s) for s in to_print_list]
        to_print = " ".join(print_strings)
        with open(self.log_file, 'a') as write_log_file:
            print(identifier, str(to_print), file=write_log_file, flush=True)

    def _initiate_database(self):
        self.db, self.legacy_db, self.model_lists = \
            database_launch.launch_db(
                true_op_name=self.true_model_name,
                new_model_branches=self.InitialModelBranches,
                new_model_ids=self.InitialModelIDs,
                log_file=self.log_file,
                gen_list=self.InitialOpsAllBranches,
                qle=self.QLE,
                true_ops=self.true_model_constituent_operators,
                true_params=self.true_param_list,
                num_particles=self.NumParticles,
                redimensionalise=False,
                resample_threshold=self.ResampleThreshold,
                resampler_a=self.ResamplerA,
                pgh_prefactor=self.PGHPrefactor,
                num_probes=self.NumProbes,
                probe_dict=self.ProbeDict,
                use_exp_custom=self.UseExpCustom,
                enable_sparse=self.EnableSparse,
                debug_directory=self.DebugDirectory,
                qid=self.qmla_id,
                host_name=self.redis_host_name,
                port_number=self.redis_port_number
            )

        for mod in list(self.InitialModelIDs.keys()):
            mod_id = self.InitialModelIDs[mod]
            if database_framework.alph(mod) == self.true_model_name:
                self.TrueOpModelID = mod_id
            print("mod id:", mod_id)
            self.model_name_id_map[int(mod_id)] = mod

        self.log_print(
            [
                "After initiating DB, models:", self.model_name_id_map
            ]
        )

    ##########
    # Section: Setup, configuration and branch/database management functions
    ##########

    def add_model_to_database(
        self,
        model,
        branchID=0,
        force_create_model=False
    ):
        #self.NumModels += 1
        model = database_framework.alph(model)
        tryAddModel = database_launch.add_model(
            model_name=model,
            running_database=self.db,
            num_particles=self.NumParticles,
            true_op_name=self.true_model_name,
            model_lists=self.model_lists,
            true_ops=self.true_model_constituent_operators,
            true_params=self.true_param_list,
            branchID=branchID,
            resample_threshold=self.ResampleThreshold,
            resampler_a=self.ResamplerA,
            pgh_prefactor=self.PGHPrefactor,
            num_probes=self.NumProbes,
            probe_dict=self.ProbeDict,
            use_exp_custom=self.UseExpCustom,
            enable_sparse=self.EnableSparse,
            debug_directory=self.DebugDirectory,
            modelID=self.NumModels,
            redimensionalise=False,
            qle=self.QLE,
            host_name=self.redis_host_name,
            port_number=self.redis_port_number,
            qid=self.qmla_id,
            log_file=self.log_file,
            force_create_model=force_create_model,
        )
        if tryAddModel == True:  # keep track of how many models/branches in play
            if database_framework.alph(model) == database_framework.alph(self.true_model_name):
                self.TrueOpModelID = self.NumModels
            self.HighestModelID += 1
            # print("Setting model ", model, "to ID:", self.NumModels)
            model_id = self.NumModels
            self.model_name_id_map[model_id] = model
            self.NumModels += 1
            # if database_framework.get_num_qubits(model) > self.HighestQubitNumber:
            #     self.HighestQubitNumber = database_framework.get_num_qubits(model)
            #     self.BranchGrowthClasses[branchID].highest_num_qubits = database_framework.get_num_qubits(
            #         model)
            #     # self.growth_class.highest_num_qubits = database_framework.get_num_qubits(model)
            #     print("self.growth_class.highest_num_qubits",
            #           self.BranchGrowthClasses[branchID].highest_num_qubits)

        # retrieve model_id from database? or somewhere
        else:
            try:
                model_id = database_framework.model_id_from_name(
                    db=self.db,
                    name=model
                )
            except BaseException:
                self.log_print(
                    [
                        "Couldn't find model id for model:", model,
                        "model_names_ids:",
                        self.model_name_id_map
                    ]
                )
                raise

        add_model_output = {
            'is_new_model': tryAddModel,
            'model_id': model_id,
        }

        return add_model_output

    def delete_unpicklable_attributes(self):
        del self.redis_conn
        del self.rq_queue
        del self.redis_databases
        del self.write_log_file

    def new_branch(
        self,
        growth_rule,
        model_list
    ):
        model_list = list(set(model_list))  # remove possible duplicates
        self.HighestBranchID += 1
        branchID = int(self.HighestBranchID)
        print(
            "NEW BRANCH {}. growth rule= {}".format(branchID, growth_rule)
        )
        self.BranchBayesComputed[branchID] = False
        num_models = len(model_list)
        self.NumModelsPerBranch[branchID] = num_models
        self.NumModelPairsPerBranch[branchID] = num_pairs_in_list(
            num_models
        )
        self.BranchAllModelsLearned[branchID] = False
        self.BranchComparisonsComplete[branchID] = False
        self.Branchget_growth_rule[branchID] = growth_rule
        self.BranchGrowthClasses[branchID] = self.UniqueGrowthClasses[growth_rule]

        self.log_print(
            [
                'Branch {} growth rule {} has {} new models {}'.format(
                    branchID,
                    growth_rule,
                    num_models,
                    model_list
                )
            ]
        )
        pre_computed_models = []
        num_models_already_computed_this_branch = 0
        model_id_list = []

        for model in model_list:
            # addModel returns whether adding model was successful
            # if false, that's because it's already been computed
            add_model_info = self.add_model_to_database(
                model,
                branchID=branchID
            )
            already_computed = not(
                add_model_info['is_new_model']
            )
            model_id = add_model_info['model_id']
            model_id_list.append(model_id)
            if already_computed == False:  # first instance of this model
                self.ModelsBranches[model_id] = branchID

            self.log_print(
                [
                    'Model ', model,
                    '\n\tcomputed already: ', already_computed,
                    '\n\tID:', model_id
                ]
            )
            num_models_already_computed_this_branch += bool(
                already_computed
            )
            if bool(already_computed) == True:
                pre_computed_models.append(model)

        self.BranchNumModelsPreComputed[branchID] = num_models_already_computed_this_branch
        self.BranchModels[branchID] = model_list
        self.BranchPrecomputedModels[branchID] = pre_computed_models

        self.BranchModelIds[branchID] = model_id_list
        self.log_print(
            [
                'Num models already computed on branch ',
                branchID,
                '=', num_models_already_computed_this_branch,
                # 'Branch model ids:', model_id_list
            ]
        )
        return branchID

    def get_model_storage_instance_by_id(self, model_id):
        return database_framework.reduced_model_instance_from_id(self.db, model_id)

    def update_database_model_info(self):
        for mod_id in range(self.HighestModelID):
            try:
                # TODO remove this try/except when reduced-champ-model instance
                # is update-able
                mod = self.get_model_storage_instance_by_id(mod_id)
                mod.updateLearnedValues(
                    fitness_parameters=self.FitnessParameters
                )
            except BaseException:
                pass

    def update_model_record(
        self,
        field,
        name=None,
        model_id=None,
        new_value=None,
        increment=None
    ):
        database_framework.update_field(
            db=self.db,
            name=name,
            model_id=model_id,
            field=field,
            new_value=new_value,
            increment=increment
        )

    def get_model_data_by_field(self, name, field):
        return database_framework.pull_field(self.db, name, field)

    def change_model_status(self, model_name, new_status='Saturated'):
        self.db.loc[self.db['<Name>'] == model_name, 'Status'] = new_status

    ##########
    # Section: Calculation of models parameters and Bayes factors
    ##########

    def learn_models_on_given_branch(
        self,
        branchID,
        use_rq=True,
        blocking=False
    ):
        model_list = self.BranchModels[branchID]
        self.log_print(
            [
                "learnModelFromBranchID branch",
                branchID,
                ":",
                model_list
            ]
        )
        active_branches_learning_models = (
            self.redis_databases['active_branches_learning_models']
        )
        num_models_already_set_this_branch = (
            self.BranchNumModelsPreComputed[branchID]
        )

        self.log_print(
            [
                "learnModelFromBranchID.",
                "Setting active branches on redis for branch",
                branchID,
                "to",
                num_models_already_set_this_branch

            ]
        )

        active_branches_learning_models.set(
            int(branchID),
            num_models_already_set_this_branch
        )

        unlearned_models_this_branch = list(
            set(model_list) -
            set(self.BranchPrecomputedModels[branchID])
        )

        self.log_print(
            [
                "branch {} precomputed:".format(
                    branchID,
                    self.BranchPrecomputedModels[branchID]
                )
            ]
        )
        if len(unlearned_models_this_branch) == 0:
            self.ghost_branch_list.append(branchID)

        self.log_print(
            [
                "Branch ", branchID,
                "has unlearned models:",
                unlearned_models_this_branch
            ]
        )

        for model_name in unlearned_models_this_branch:
            self.log_print(
                [
                    "Model ", model_name,
                    "being passed to learnModel function"
                ]
            )
            self.learn_model(
                model_name=model_name,
                use_rq=self.use_rq,
                blocking=blocking
            )
            if blocking is True:
                self.log_print(
                    [
                        "Blocking on; model finished:",
                        model_name
                    ]
                )
            self.update_model_record(
                field='Completed',
                name=model_name,
                new_value=True
            )
        self.log_print(
            [
                'learnModelFromBranchID finished, branch', branchID
            ]
        )

    def learn_model(
        self,
        model_name,
        use_rq=True,
        blocking=False
    ):
        exists = database_framework.check_model_exists(
            model_name=model_name,
            model_lists=self.model_lists,
            db=self.db
        )
        if exists:
            modelID = database_framework.model_id_from_name(
                self.db,
                name=model_name
            )

            branchID = self.ModelsBranches[modelID]

            if self.RunParallel and use_rq:
                # i.e. use a job queue rather than sequentially doing it.
                from rq import Connection, Queue, Worker
                queue = Queue(
                    self.qmla_id,
                    connection=self.redis_conn,
                    async=self.use_rq,
                    default_timeout=self.rq_timeout
                )  # TODO is this timeout sufficient for ALL QMD jobs?

                # add function call to RQ queue
                print("[QMD 1085] RQ used")
                queued_model = queue.enqueue(
                    learnModelRemote,
                    model_name,
                    modelID,
                    growth_generator=self.Branchget_growth_rule[branchID],
                    branchID=branchID,
                    remote=True,
                    host_name=self.redis_host_name,
                    port_number=self.redis_port_number,
                    qid=self.qmla_id,
                    log_file=self.rq_log_file,
                    result_ttl=-1,
                    timeout=self.rq_timeout
                )

                self.log_print(
                    [
                        "Model",
                        model_name,
                        "added to queue."
                    ]
                )
                if blocking == True:  # i.e. wait for result when called.
                    self.log_print(
                        [
                            "Blocking, ie waiting for",
                            model_name,
                            "to finish on redis queue."
                        ]
                    )
                    while not queued_model.is_finished:
                        if queued_model.is_failed:
                            self.log_print(
                                [
                                    "Model", model_name,
                                    "has failed on remote worker."
                                ]
                            )
                            raise NameError("Remote QML failure")
                            break
                        time.sleep(0.1)
                    self.log_print(
                        ['Blocking RQ model learned:', model_name]
                    )

            else:
                self.log_print(
                    [
                        "Locally calling learn model function.",
                        "model:", model_name
                    ]
                )
                self.QMDInfo['probe_dict'] = self.ProbeDict
                updated_model_info = learnModelRemote(
                    model_name,
                    modelID,
                    growth_generator=self.Branchget_growth_rule[branchID],
                    branchID=branchID,
                    qmd_info=self.QMDInfo,
                    remote=True,
                    host_name=self.redis_host_name,
                    port_number=self.redis_port_number,
                    qid=self.qmla_id, log_file=self.rq_log_file
                )

                del updated_model_info
        else:
            self.log_print(
                [
                    "Model",
                    model_name,
                    "does not yet exist."
                ]
            )

    def get_pairwise_bayes_factor(
        self,
        model_a_id,
        model_b_id,
        return_job=False,
        branchID=None,
        interbranch=False,
        remote=True,
        bayes_threshold=None,
        wait_on_result=False
    ):
        if bayes_threshold is None:
            bayes_threshold = self.BayesUpper

        if branchID is None:
            interbranch = True
        unique_id = database_framework.unique_model_pair_identifier(
            model_a_id,
            model_b_id
        )
        if (
            unique_id not in self.bayes_factor_pair_computed
        ):  # ie not yet considered
            self.bayes_factor_pair_computed.append(
                unique_id
            )

        if self.use_rq:
            from rq import Connection, Queue, Worker
            queue = Queue(self.qmla_id, connection=self.redis_conn,
                          async=self.use_rq, default_timeout=self.rq_timeout
                          )
            job = queue.enqueue(
                BayesFactorRemote,
                model_a_id=model_a_id,
                model_b_id=model_b_id,
                branchID=branchID,
                interbranch=interbranch,
                times_record=self.BayesFactorsTimeFile,
                bf_data_folder=self.BayesFactorsFolder,
                num_times_to_use=self.NumTimesForBayesUpdates,
                trueModel=self.true_model_name,
                bayes_threshold=bayes_threshold,
                host_name=self.redis_host_name,
                port_number=self.redis_port_number,
                qid=self.qmla_id,
                log_file=self.rq_log_file,
                result_ttl=-1,
                timeout=self.rq_timeout
            )
            self.log_print(
                [
                    "Bayes factor calculation queued. Model IDs",
                    model_a_id,
                    model_b_id
                ]
            )
            if wait_on_result == True:
                while job.is_finished == False:
                    if job.is_failed == True:
                        raise("Remote BF failure")
                    sleep(0.1)
            elif return_job == True:
                return job
        else:
            BayesFactorRemote(
                model_a_id=model_a_id,
                model_b_id=model_b_id,
                trueModel=self.true_model_name,
                bf_data_folder=self.BayesFactorsFolder,
                times_record=self.BayesFactorsTimeFile,
                num_times_to_use=self.NumTimesForBayesUpdates,
                branchID=branchID,
                interbranch=interbranch,
                bayes_threshold=bayes_threshold,
                host_name=self.redis_host_name,
                port_number=self.redis_port_number,
                qid=self.qmla_id,
                log_file=self.rq_log_file
            )
        if wait_on_result == True:
            pair_id = database_framework.unique_model_pair_identifier(
                model_a_id,
                model_b_id
            )
            bf_from_db = self.redis_databases['bayes_factors_db'].get(pair_id)
            bayes_factor = float(bf_from_db)

            return bayes_factor

    def get_bayes_factors_from_list(
        self,
        model_id_list,
        remote=True,
        wait_on_result=False,
        recompute=False,
        bayes_threshold=None
    ):
        if bayes_threshold is None:
            bayes_threshold = self.BayesLower

        remote_jobs = []
        num_models = len(model_id_list)
        for i in range(num_models):
            a = model_id_list[i]
            for j in range(i, num_models):
                b = model_id_list[j]
                if a != b:
                    unique_id = database_framework.unique_model_pair_identifier(a, b)
                    if (
                        unique_id not in self.bayes_factor_pair_computed
                        or recompute == True
                    ):  # ie not yet considered
                        # self.bayes_factor_pair_computed.append(
                        #     unique_id
                        # )
                        remote_jobs.append(
                            self.get_pairwise_bayes_factor(
                                a,
                                b,
                                remote=remote,
                                return_job=wait_on_result,
                                bayes_threshold=bayes_threshold
                            )
                        )

        if wait_on_result and self.use_rq:  # test_workers from redis_settings
            self.log_print(
                [
                    "Waiting on result of ",
                    "Bayes comparisons from given list:",
                    model_id_list
                ]

            )
            for job in remote_jobs:
                while job.is_finished == False:
                    if job.is_failed == True:
                        raise NameError("Remote QML failure")
                    time.sleep(0.01)
        else:
            self.log_print(
                [
                    "Not waiting on results",
                    "since not using RQ workers."
                ]
            )

    def get_bayes_factors_by_branch_id(
        self,
        branchID,
        remote=True,
        bayes_threshold=None,  # actually was 50,
        recompute=False
    ):
        if bayes_threshold is None:
            bayes_threshold = self.BayesUpper

        active_branches_bayes = self.redis_databases['active_branches_bayes']
        # model_id_list = database_framework.active_model_ids_by_branch_id(self.db, branchID)
        model_id_list = self.BranchModelIds[branchID]
        self.log_print(
            [
                'get_bayes_factors_by_branch_id',
                branchID,
                'model id list:',
                model_id_list
            ]
        )

        active_branches_bayes.set(int(branchID), 0)  # set up branch 0
        num_models = len(model_id_list)
        for i in range(num_models):
            a = model_id_list[i]
            for j in range(i, num_models):
                b = model_id_list[j]
                if a != b:
                    unique_id = database_framework.unique_model_pair_identifier(a, b)
                    if (
                        unique_id not in self.bayes_factor_pair_computed
                        or
                        recompute == True
                    ):  # ie not yet considered
                        # self.bayes_factor_pair_computed.append(unique_id)
                        self.log_print(
                            [
                                "Computing BF for pair",
                                unique_id
                            ]
                        )

                        self.get_pairwise_bayes_factor(
                            a,
                            b,
                            remote=remote,
                            branchID=branchID,
                            bayes_threshold=bayes_threshold
                        )
                    elif unique_id in self.bayes_factor_pair_computed:
                        # if this already computed, so we need to tell this
                        # branch not to wait on it.
                        active_branches_bayes.incr(
                            int(branchID),
                            1
                        )
                        self.log_print(
                            [
                                "BF already computed for pair ",
                                unique_id,
                                "now active branches bayes, br",
                                branchID,
                                ":",
                                active_branches_bayes[branchID]
                            ]
                        )

    def processRemoteBayesPair(
        self,
        a=None,
        b=None,
        pair=None,
        bayes_threshold=None
    ):

        if bayes_threshold is None:
            bayes_threshold = self.BayesLower
        bayes_factors_db = self.redis_databases['bayes_factors_db']
        if pair is not None:
            model_ids = pair.split(',')
            a = (float(model_ids[0]))
            b = (float(model_ids[1]))
        elif a is not None and b is not None:
            a = float(a)
            b = float(b)
            pair = database_framework.unique_model_pair_identifier(a, b)
        else:
            self.log_print(
                [
                    "Must pass either two model ids, or a \
                pair name string, to process Bayes factors."]
            )
        try:
            bayes_factor = float(
                bayes_factors_db.get(pair)
            )
        except TypeError:
            self.log_print(
                [
                    "On bayes_factors_db for pair id",
                    pair,
                    "value=",
                    bayes_factors_db.get(pair)
                ]
            )

        # bayes_factor refers to calculation BF(pair), where pair
        # is defined (lower, higher) for continuity
        lower_id = min(a, b)
        higher_id = max(a, b)

        mod_low = self.get_model_storage_instance_by_id(lower_id)
        mod_high = self.get_model_storage_instance_by_id(higher_id)
        if higher_id in mod_low.BayesFactors:
            mod_low.BayesFactors[higher_id].append(bayes_factor)
        else:
            mod_low.BayesFactors[higher_id] = [bayes_factor]

        if lower_id in mod_high.BayesFactors:
            mod_high.BayesFactors[lower_id].append((1.0 / bayes_factor))
        else:
            mod_high.BayesFactors[lower_id] = [(1.0 / bayes_factor)]

        if bayes_factor > bayes_threshold:
            champ = mod_low.ModelID
        elif bayes_factor < (1.0 / bayes_threshold):
            champ = mod_high.ModelID

        return champ


    def compare_all_models_in_branch(
        self,
        branchID,
        bayes_threshold=None
    ):

        active_models_in_branch_old = database_framework.active_model_ids_by_branch_id(
            self.db,
            branchID
        )
        active_models_in_branch = self.BranchModelIds[branchID]
        self.log_print(
            [
                'compare_all_models_in_branch', branchID,
                'active_models_in_branch_old:', active_models_in_branch_old,
                'active_models_in_branch_new:', active_models_in_branch,
            ]
        )

        if bayes_threshold is None:
            bayes_threshold = self.BayesLower

        models_points = {}
        for model_id in active_models_in_branch:
            models_points[model_id] = 0

        for i in range(len(active_models_in_branch)):
            mod1 = active_models_in_branch[i]
            for j in range(i, len(active_models_in_branch)):
                mod2 = active_models_in_branch[j]
                if mod1 != mod2:
                    res = self.processRemoteBayesPair(a=mod1, b=mod2)
                    models_points[res] += 1
                    self.log_print(
                        [
                            "[compare_all_models_in_branch {}]".format(branchID),
                            "Point to", res,
                            "(comparison {}/{})".format(mod1, mod2),
                        ]
                    )
                    # if res == "a":
                    #     models_points[mod1] += 1
                    #     losing_model_id = mod2
                    # elif res == "b":
                    #     models_points[mod2] += 1
                    #     losing_model_id = mod1
                    # todo if more than one model has max points

        max_points = max(models_points.values())
        max_points_branches = [
            key for key, val in models_points.items()
            if val == max_points
        ]

        if len(max_points_branches) > 1:
            # todo: recompare. Fnc: compareListOfModels (rather than branch
            # based)
            self.log_print(
                [
                    "Multiple models have same number of points within \
                    branch.\n",
                    models_points
                ]
            )
            self.get_bayes_factors_from_list(
                model_id_list=max_points_branches,
                remote=True,
                recompute=True,
                bayes_threshold=bayes_threshold,
                wait_on_result=True
            )

            champ_id = self.compare_models_from_list(
                max_points_branches,
                bayes_threshold=bayes_threshold,
                models_points_dict=models_points
            )
        else:
            champ_id = max(models_points, key=models_points.get)
        champ_id = int(champ_id)
        # champ_name = database_framework.model_name_from_id(
        #     self.db,
        #     champ_id
        # )
        champ_name = self.model_name_id_map[champ_id]

        champ_num_qubits = database_framework.get_num_qubits(champ_name)
        self.BranchChampions[int(branchID)] = champ_id
        if champ_id not in self.ActiveBranchChampList:
            self.ActiveBranchChampList.append(champ_id)
        growth_rule = self.Branchget_growth_rule[int(branchID)]
        try:
            self.BranchChampsByNumQubits[growth_rule][champ_num_qubits].append(
                champ_name)
        except BaseException:
            self.BranchChampsByNumQubits[growth_rule][champ_num_qubits] = [
                champ_name]

        for model_id in active_models_in_branch:
            self.update_model_record(
                model_id=model_id,
                field='Status',
                new_value='Deactivated'
            )

        self.update_model_record(
            # name=database_framework.model_name_from_id(self.db, champ_id),
            name=self.model_name_id_map[champ_id],
            field='Status',
            new_value='Active'
        )
        ranked_model_list = sorted(
            models_points,
            key=models_points.get,
            reverse=True
        )

        if self.BranchBayesComputed[int(float(branchID))] == False:
            # only update self.branch_rankings the first time branch is
            # considered
            self.branch_rankings[int(float(branchID))] = ranked_model_list
            self.BranchBayesComputed[int(float(branchID))] = True

        self.log_print(
            [
                "Model points for branch",
                branchID,
                models_points
            ]
        )
        self.log_print(
            [
                "Champion of branch ",
                branchID,
                " is ",
                champ_name,
                "({})".format(champ_id)
            ]
        )
        self.branch_bayes_points[branchID] = models_points
        # self.Branchget_growth_rule[branchID]

        if branchID in self.ghost_branch_list:
            models_to_deactivate = list(
                set(active_models_in_branch)
                - set([champ_id])
            )
            # Ghost branches are to compare
            # already computed models from
            # different branches.
            # So deactivate losers since they shouldn't
            # progress if they lose in a ghost branch.
            for losing_model_id in models_to_deactivate:
                try:
                    self.update_model_record(
                        model_id=losing_model_id,
                        field='Status',
                        new_value='Deactivated'
                    )
                except BaseException:
                    self.log_print(
                        [
                            "not deactivating",
                            losing_model_id,
                            "ActiveBranchChampList:",
                            self.ActiveBranchChampList
                        ]
                    )
                try:
                    self.ActiveBranchChampList.remove(
                        losing_model_id
                    )
                    self.log_print(
                        [
                            "Ghost Branch",
                            branchID,
                            "deactivating model",
                            losing_model_id
                        ]
                    )
                except BaseException:
                    pass
        return models_points, champ_id

    def compare_models_from_list(
        self,
        model_list,
        bayes_threshold=None,
        models_points_dict=None,
        num_times_to_use='all'
    ):
        if bayes_threshold is None:
            bayes_threshold = self.BayesLower

        models_points = {}
        for mod in model_list:
            models_points[mod] = 0

        for i in range(len(model_list)):
            mod1 = model_list[i]
            for j in range(i, len(model_list)):
                mod2 = model_list[j]
                if mod1 != mod2:

                    res = self.processRemoteBayesPair(a=mod1, b=mod2)
                    if res == mod1:
                        loser = mod2
                    elif res == mod2:
                        loser = mod1
                    models_points[res] += 1
                    self.log_print(
                        [
                            "[compare_models_from_list]",
                            "Point to", res,
                            "(comparison {}/{})".format(mod1, mod2)
                        ]
                    )

        max_points = max(models_points.values())
        max_points_branches = [key for key, val in models_points.items()
                               if val == max_points]
        if len(max_points_branches) > 1:
            self.log_print(
                [
                    "Multiple models \
                    have same number of points in compare_models_from_list:",
                    max_points_branches
                ]
            )
            self.log_print(["Recompute Bayes bw:"])
            for i in max_points_branches:
                self.log_print(
                    [
                        database_framework.model_name_from_id(self.db, i)
                    ]
                )
            self.log_print(["Points:\n", models_points])
            self.get_bayes_factors_from_list(
                model_id_list=max_points_branches,
                remote=True,
                recompute=True,
                bayes_threshold=self.BayesLower,
                wait_on_result=True
            )
            champ_id = self.compare_models_from_list(
                max_points_branches,
                bayes_threshold=self.BayesLower
            )
        else:
            self.log_print(["After comparing list:", models_points])
            champ_id = max(models_points, key=models_points.get)
        # champ_name = database_framework.model_name_from_id(self.db, champ_id)
        champ_name = self.model_name_id_map[champ_id]

        return champ_id

    def perform_final_bayes_comparisons(
        self,
        bayes_threshold=None
    ):
        if bayes_threshold is None:
            bayes_threshold = self.BayesUpper

        bayes_factors_db = self.redis_databases['bayes_factors_db']
        # branch_champions = list(self.BranchChampions.values())
        branch_champions = self.ActiveBranchChampList
        job_list = []
        job_finished_count = 0
        # if a spawned model is this much better than its parent, parent is
        # deactivated
        interbranch_collapse_threshold = 1e5
        # interbranch_collapse_threshold = 3 ## if a spawned model is this much
        # better than its parent, parent is deactivated
        num_champs = len(branch_champions)

        self.log_print(
            [
                "Active branch champs at start of final Bayes comp:",
                self.ActiveBranchChampList
            ]
        )
        children_branches = list(self.branch_parents.keys())
        for child_id in branch_champions:
            # child_id = branch_champions[k]
            # branch this child sits on
            child_branch = self.ModelsBranches[child_id]

            try:
                # TODO make parent relationships more explicit by model rather
                # than alway parent branch champ
                parent_branch = self.branch_parents[child_branch]
                parent_id = self.BranchChampions[parent_branch]

                if (
                    child_id in self.ActiveBranchChampList
                    and
                    parent_id in self.ActiveBranchChampList
                ):

                    job_list.append(
                        self.get_pairwise_bayes_factor(
                            model_a_id=parent_id,
                            model_b_id=child_id,
                            return_job=True,
                            remote=self.use_rq
                        )
                    )

                    self.log_print(
                        [
                            "Comparing child ",
                            child_id,
                            "with parent",
                            parent_id
                        ]
                    )
                else:
                    self.log_print(
                        [
                            "Either parent or child not in ActiveBranchChampList",
                            "Child:", child_id,
                            "Parent:", parent_id
                        ]
                    )
            except BaseException:
                self.log_print(
                    [
                        "Model",
                        child_id,
                        "doesn't have a parent to compare with."
                    ]
                )

        self.log_print(
            [
                "Final Bayes Comparisons.",
                "\nEntering while loop in final bayes fnc.",
                "\nactive branch champs: ", branch_champions

            ]
        )

        if self.use_rq:
            self.log_print(
                [
                    "Waiting on parent/child Bayes factors."
                ]
            )
            for k in range(len(job_list)):
                self.log_print(
                    [
                        "Waiting on parent/child Bayes factors."
                    ]
                )
                while job_list[k].is_finished == False:
                    if job_list[k].is_failed == True:
                        raise NameError("Remote QML failure")
                    sleep(0.01)
            self.log_print(
                [
                    "Parent/child Bayes factors jobs all launched."
                ]
            )

        else:
            self.log_print(
                [
                    "Jobs all finished because not on RQ"
                ]
            )
        # now deactivate parent/children based on those bayes factors
        models_to_remove = []
        for child_id in branch_champions:
            # child_id = branch_champions[k]
            # branch this child sits on
            child_branch = self.ModelsBranches[child_id]
            try:
                parent_branch = self.branch_parents[child_branch]
                parent_id = self.BranchChampions[parent_branch]

                mod1 = min(parent_id, child_id)
                mod2 = max(parent_id, child_id)

                pair_id = database_framework.unique_model_pair_identifier(
                    mod1,
                    mod2
                )
                bf_from_db = bayes_factors_db.get(pair_id)
                bayes_factor = float(bf_from_db)
                self.log_print(
                    [
                        "parent/child {}/{} has bf {}".format(
                            parent_id,
                            child_id,
                            bayes_factor
                        )
                    ]
                )

                if bayes_factor > interbranch_collapse_threshold:
                    # bayes_factor heavily favours mod1, so deactive mod2
                    self.log_print(
                        [
                            "Parent model,",
                            mod1,
                            "stronger than spawned; deactivating model",
                            mod2
                        ]
                    )
                    self.update_model_record(
                        model_id=mod2,
                        field='Status',
                        new_value='Deactivated'
                    )
                    try:
                        models_to_remove.append(mod2)
                        # self.ActiveBranchChampList.remove(mod2)
                    except BaseException:
                        pass
                elif bayes_factor < (1.0 / interbranch_collapse_threshold):
                    self.log_print(
                        [
                            "Spawned model",
                            mod2,
                            "stronger than parent; deactivating model",
                            mod1
                        ]
                    )
                    self.update_model_record(
                        model_id=mod1,
                        field='Status',
                        new_value='Deactivated'
                    )
                    try:
                        models_to_remove.append(mod1)
                        # self.ActiveBranchChampList.remove(mod1)
                    except BaseException:
                        pass

                # Add bayes factors to BayesFactor dict for each model
                mod_a = self.get_model_storage_instance_by_id(mod1)
                mod_b = self.get_model_storage_instance_by_id(mod2)
                if mod2 in mod_a.BayesFactors:
                    mod_a.BayesFactors[mod2].append(bayes_factor)
                else:
                    mod_a.BayesFactors[mod2] = [bayes_factor]

                if mod1 in mod_b.BayesFactors:
                    mod_b.BayesFactors[mod1].append((1.0 / bayes_factor))
                else:
                    mod_b.BayesFactors[mod1] = [(1.0 / bayes_factor)]
            except Exception as exc:
                self.log_print(
                    [
                        "child doesn't have active parent",
                        # "\t child id ", child_id,
                        # "\t parent id ", parent_id,
                        # "\n\tchild branch:", child_branch,
                        # "\tparent branch:", parent_branch
                    ]
                )
                self.log_print(
                    [
                        "Error:", exc
                    ]
                )
                # raise
        self.ActiveBranchChampList = list(
            set(self.ActiveBranchChampList) -
            set(models_to_remove)
        )
        self.log_print(
            [
                "Parent/child comparisons and deactivations complete."
            ]
        )
        # for k in range(num_champs - 1):
        #     mod1 = branch_champions[k]
        #     mod2 = branch_champions[k+1]
        self.log_print(
            [
                "Active branch champs after ",
                "parental collapse (final Bayes comp):",
                self.ActiveBranchChampList
            ]
        )
        # make ghost branches of all individidual trees
        # individual trees correspond to separate growth rules.
        self.ActiveTreeBranchChamps = {}
        for gen in self.growth_rules_list:
            self.ActiveTreeBranchChamps[gen] = []

        for active_champ in self.ActiveBranchChampList:
            branch_id_of_champ = self.ModelsBranches[active_champ]
            gen = self.Branchget_growth_rule[branch_id_of_champ]
            self.ActiveTreeBranchChamps[gen].append(active_champ)

        self.log_print(
            [
                "ActiveTreeBranchChamps:",
                self.ActiveTreeBranchChamps
            ]
        )
        self.FinalTrees = []
        for gen in list(self.ActiveTreeBranchChamps.keys()):
            models_for_tree_ghost_branch = self.ActiveTreeBranchChamps[gen]
            mod_names = [
                self.model_name_id_map[m]
                for m in models_for_tree_ghost_branch
            ]
            new_branch_id = self.new_branch(
                model_list=mod_names,
                growth_rule=gen
            )

            self.FinalTrees.append(
                new_branch_id
            )
            self.BranchAllModelsLearned[new_branch_id] = True
            self.learn_models_on_given_branch(new_branch_id)
            self.get_bayes_factors_by_branch_id(new_branch_id)
            # self.get_bayes_factors_by_branch_id(new_branch_id)

        active_branches_learning_models = (
            self.redis_databases[
                'active_branches_learning_models'
            ]
        )
        active_branches_bayes = self.redis_databases[
            'active_branches_bayes'
        ]
        still_learning = True

        # print("[QMD]Entering final while loop")
        while still_learning:
            branch_ids_on_db = list(
                active_branches_learning_models.keys()
            )
            for branchID_bytes in branch_ids_on_db:
                branchID = int(branchID_bytes)
                if (
                    (int(active_branches_learning_models.get(branchID)) ==
                     self.NumModelsPerBranch[branchID])
                    and
                    (self.BranchAllModelsLearned[branchID] == False)
                ):
                    self.BranchAllModelsLearned[branchID] = True
                    self.get_bayes_factors_by_branch_id(branchID)

                if branchID_bytes in active_branches_bayes.keys():
                    branchID = int(branchID_bytes)
                    num_bayes_done_on_branch = (
                        active_branches_bayes.get(branchID_bytes)
                    )

                    if (int(num_bayes_done_on_branch) ==
                        self.NumModelPairsPerBranch[branchID]
                            and
                            self.BranchComparisonsComplete[branchID] == False
                        ):
                        self.BranchComparisonsComplete[branchID] = True
                        self.compare_all_models_in_branch(branchID)

            if (
                np.all(
                    np.array(list(self.BranchAllModelsLearned.values()))
                    == True
                )
                and
                np.all(np.array(list(
                    self.BranchComparisonsComplete.values())) == True
                )
            ):
                still_learning = False  # i.e. break out of this while loop

        self.log_print(["Final tree comparisons complete."])

        # Finally, compare all remaining active models,
        # which should just mean the tree champions at this point.
        active_models = database_framework.all_active_model_ids(self.db)
        self.SurvivingChampions = database_framework.all_active_model_ids(
            self.db
        )
        self.log_print(
            [
                "After initial interbranch comparisons, \
                remaining active branch champions:",
                active_models
            ]
        )
        num_active_models = len(active_models)

        self.get_bayes_factors_from_list(
            model_id_list=active_models,
            remote=True,
            recompute=True,
            wait_on_result=True,
            bayes_threshold=bayes_threshold
        )

        branch_champions_points = {}
        for c in active_models:
            branch_champions_points[c] = 0

        for i in range(num_active_models):
            mod1 = active_models[i]
            for j in range(i, num_active_models):
                mod2 = active_models[j]
                if mod1 != mod2:
                    res = self.processRemoteBayesPair(
                        a=mod1,
                        b=mod2
                    )
                    self.log_print(
                        [
                            "[perform_final_bayes_comparisons]",
                            "Point to", res,
                            "(comparison {}/{})".format(mod1, mod2)
                        ]
                    )

                    branch_champions_points[res] += 1
                    # if res == "a":
                    #     branch_champions_points[mod1] += 1
                    # elif res == "b":
                    #     branch_champions_points[mod2] += 1
        self.ranked_champions = sorted(
            branch_champions_points,
            reverse=True
        )
        self.log_print(
            [
                "After final Bayes comparisons (of branch champions)",
                branch_champions_points
            ]
        )

        max_points = max(branch_champions_points.values())
        max_points_branches = [
            key for key, val in branch_champions_points.items()
            if val == max_points
        ]
        if len(max_points_branches) > 1:
            # todo: recompare. Fnc: compareListOfModels (rather than branch
            # based)
            self.log_print(
                [
                    "No distinct champion, recomputing bayes "
                    "factors between : ",
                    max_points_branches
                ]
            )
            champ_id = self.compare_models_from_list(
                max_points_branches,
                bayes_threshold=self.BayesLower,
                models_points_dict=branch_champions_points
            )
        else:
            champ_id = max(
                branch_champions_points,
                key=branch_champions_points.get
            )
        # champ_name = database_framework.model_name_from_id(self.db, champ_id)
        champ_name = self.model_name_id_map[champ_id]

        branch_champ_names = [
            # database_framework.model_name_from_id(self.db, mod_id)
            self.model_name_id_map[mod_id]
            for mod_id in active_models
        ]
        self.change_model_status(
            champ_name,
            new_status='Active'
        )
        return champ_name, branch_champ_names

    ##########
    # Section: QMLA algorithm subroutines
    ##########

    def spawn_from_branch(
        self,
        branchID,
        growth_rule,
        num_models=1
    ):

        self.spawn_depthByGrowthRule[growth_rule] += 1
        self.spawn_depth += 1
        # self.log_print(["Spawning, spawn depth:", self.spawn_depth])
        self.log_print(
            [
                "Spawning. Growth rule: {}. Depth: {}".format(
                    growth_rule,
                    self.spawn_depthByGrowthRule[growth_rule]
                )
            ]
        )
        all_models_this_branch = self.branch_rankings[branchID]
        best_models = self.branch_rankings[branchID][:num_models]
        best_model_names = [
            # database_framework.model_name_from_id(self.db, mod_id) for
            self.model_name_id_map[mod_id]
            for mod_id in best_models
        ]
        # new_models = model_generation.new_model_list(
        current_champs = [
            self.model_name_id_map[i] for i in
            list(self.BranchChampions.values())
        ]
        # print("[QMD] fitness parameters:", self.FitnessParameters)

        new_models = self.BranchGrowthClasses[branchID].generate_models(
            # generator = growth_rule,
            model_list=best_model_names,
            spawn_step=self.spawn_depthByGrowthRule[growth_rule],
            ghost_branches=self.GhostBranches,
            branch_champs_by_qubit_num=self.BranchChampsByNumQubits[growth_rule],
            model_dict=self.model_lists,
            log_file=self.log_file,
            current_champs=current_champs,
            spawn_stage=self.SpawnStage[growth_rule],
            branch_model_points=self.branch_bayes_points[branchID],
            model_names_ids=self.model_name_id_map,
            miscellaneous=self.MiscellaneousGrowthInfo[growth_rule]
        )
        new_models = list(set(new_models))
        new_models = [database_framework.alph(mod) for mod in new_models]

        self.log_print(
            [
                "After model generation for growth rule",
                growth_rule,
                "SPAWN STAGE:",
                self.SpawnStage[growth_rule],
                "\nnew models:",
                new_models
            ]
        )

        new_branch_id = self.new_branch(
            model_list=new_models,
            growth_rule=growth_rule
        )

        self.branch_parents[new_branch_id] = branchID

        self.log_print(
            [
                "Models to add to new branch (",
                new_branch_id,
                "): ",
                new_models
            ]
        )

        try:
            new_model_dimension = database_framework.get_num_qubits(
                new_models[0]
            )
        except BaseException:
            # TODO this is only during development -- only for cases where
            # spawn step determines termination
            new_model_dimension = database_framework.get_num_qubits(
                best_model_names[0]
            )

        self.learn_models_on_given_branch(
            new_branch_id,
            blocking=False,
            use_rq=True
        )
        tree_completed = self.BranchGrowthClasses[branchID].check_tree_completed(
            spawn_step=self.spawn_depthByGrowthRule[growth_rule],
            current_num_qubits=new_model_dimension
        )

        if self.SpawnStage[growth_rule][-1] == 'Complete':
            tree_completed = True
        return tree_completed

    def inspect_remote_job_crashes(self):
        if self.redis_databases['any_job_failed']['Status'] == b'1':
            # TODO better way to detect errors? For some reason the
            # log print isn't being hit, but raising error seems to be.
            self.log_print(
                [
                    "Failure on remote node. Terminating QMD."
                ]
            )
            raise NameError('Remote QML Failure')


    def finalise_qmla(self):
        # Final functions at end of QMD
        # Fill in champions result dict for further analysis.

        champ_model = self.get_model_storage_instance_by_id(self.ChampID)
        for i in range(self.HighestModelID):
            # Dict of all Bayes factors for each model considered.
            self.all_bayes_factors[i] = (
                self.get_model_storage_instance_by_id(i).BayesFactors
            )

        self.log_print(["computing expect vals for mod ", champ_model.ModelID])
        champ_model.compute_expectation_values(
            times=self.PlotTimes,
            # plot_probe_path = self.PlotProbeFile
        )
        self.log_print(["computed expect vals"])

        self.compute_f_score(
            model_id=self.ChampID
        )

        self.ChampionFinalParams = (
            champ_model.FinalParams
        )

        champ_op = database_framework.Operator(self.ChampionName)
        num_params_champ_model = champ_op.num_constituents

        correct_model = misfit = underfit = overfit = 0
        self.log_print(
            [
                "Num params - champ:", num_params_champ_model,
                "; \t true:", self.true_model_num_params]
        )

        self.ModelIDNames = {}
        for k in self.model_name_id_map:
            v = self.model_name_id_map[k]
            self.ModelIDNames[v] = k

        if database_framework.alph(self.ChampionName) == database_framework.alph(self.true_model_name):
            correct_model = 1
        elif (
            num_params_champ_model == self.true_model_num_params
            and
            database_framework.alph(self.ChampionName) != database_framework.alph(self.true_model_name)
        ):
            misfit = 1
        elif num_params_champ_model > self.true_model_num_params:
            overfit = 1
        elif num_params_champ_model < self.true_model_num_params:
            underfit = 1

        num_params_difference = self.true_model_num_params - num_params_champ_model

        num_qubits_champ_model = database_framework.get_num_qubits(self.ChampionName)
        self.LearnedParamsChamp = (
            self.get_model_storage_instance_by_id(self.ChampID).LearnedParameters
        )
        self.FinalSigmasChamp = (
            self.get_model_storage_instance_by_id(self.ChampID).FinalSigmas
        )
        num_exp_ham = (
            self.NumParticles *
            (self.NumExperiments + self.NumTimesForBayesUpdates)
        )

        config = str('config' +
                     '_p' + str(self.NumParticles) +
                     '_e' + str(self.NumExperiments) +
                     '_b' + str(self.NumTimesForBayesUpdates) +
                     '_ra' + str(self.ResamplerA) +
                     '_rt' + str(self.ResampleThreshold) +
                     '_rp' + str(self.PGHPrefactor)
                     )

        time_now = time.time()
        time_taken = time_now - self._start_time

        n_qubits = database_framework.get_num_qubits(champ_model.Name)
        if n_qubits > 3:
            # only compute subset of points for plot
            # otherwise takes too long
            self.log_print(
                [
                    "getting new set of times to plot expectation values for"
                ]
            )
            expec_val_plot_times = self.ReducedPlotTimes
        else:
            self.log_print(
                [
                    "Using default times to plot expectation values for",
                    "num qubits:", n_qubits
                ]
            )
            expec_val_plot_times = self.PlotTimes

        self.ChampLatex = champ_model.LatexTerm
        # equivalent to sleepf.ResultsDict

        self.ChampionResultsDict = {
            'NameAlphabetical': database_framework.alph(self.ChampionName),
            'NameNonAlph': self.ChampionName,
            'FinalParams': self.ChampionFinalParams,
            'LatexName': champ_model.LatexTerm,
            # 'LatexName' : database_framework.latex_name_ising(self.ChampionName),
            'NumParticles': self.NumParticles,
            'NumExperiments': champ_model.NumExperiments,
            'NumBayesTimes': self.NumTimesForBayesUpdates,
            'ResampleThreshold': self.ResampleThreshold,
            'ResamplerA': self.ResamplerA,
            'PHGPrefactor': self.PGHPrefactor,
            'LogFile': self.log_file,
            'ParamConfiguration': config,
            'ConfigLatex': self.LatexConfig,
            'Time': time_taken,
            'QID': self.qmla_id,
            'CorrectModel': correct_model,
            'Underfit': underfit,
            'Overfit': overfit,
            'Misfit': misfit,
            'NumQubits': num_qubits_champ_model,
            'NumParams': num_params_champ_model,
            'LearnedParameters': self.LearnedParamsChamp,
            'FinalSigmas': self.FinalSigmasChamp,
            'QuadraticLosses': champ_model.QuadraticLosses,
            'ExpectationValues': champ_model.expectation_values,
            # 'RawExpectationValues' : champ_model.raw_expectation_values,
            # 'ExpValTimes' : champ_model.times,
            'TrackParameterEstimates': champ_model.TrackParameterEstimates,
            'TrackVolume': champ_model.VolumeList,
            'TrackTimesLearned': champ_model.Times,
            # 'TrackCovarianceMatrices' : champ_model.TrackCovMatrices,
            # 'RSquaredByEpoch' : champ_model.r_squared_by_epoch(
            #     plot_probes = self.PlotProbes,
            #     times = expec_val_plot_times
            # ),
            'FinalRSquared': champ_model.r_squared(
                plot_probes=self.PlotProbes,
                times=expec_val_plot_times
            ),
            'Fscore': self.FScore,
            'Precision': self.Precision,
            'Sensitivity': self.Sensitivity,
            'PValue': champ_model.p_value,
            'LearnedHamiltonian': champ_model.LearnedHamiltonian,
            'GrowthGenerator': champ_model.growth_rule_of_true_model,
            'Heuristic': champ_model.HeuristicType,
            'ChampLatex': champ_model.LatexTerm,
            'TrueModel': database_framework.alph(self.true_model_name),
            'NumParamDifference': num_params_difference,
        }

    def check_champion_reducibility(
        self,
    ):
        champ_mod = self.get_model_storage_instance_by_id(self.ChampID)
        self.log_print(
            [
                "Checking reducibility of champ model:",
                self.ChampionName,
                "\nParams:\n", champ_mod.LearnedParameters,
                "\nSigmas:\n", champ_mod.FinalSigmas
            ]
        )

        params = list(champ_mod.LearnedParameters.keys())
        to_remove = []
        removed_params = {}
        idx = 0
        for p in params:
            # if champ_mod.FinalSigmas[p] > champ_mod.LearnedParameters[p]:
            #     to_remove.append(p)
            #     removed_params[p] = np.round(
            #         champ_mod.LearnedParameters[p],
            #         2
            #     )

            if (
                np.abs(champ_mod.LearnedParameters[p])
                < self.growth_class.learned_param_limit_for_negligibility
            ):
                to_remove.append(p)
                removed_params[p] = np.round(
                    champ_mod.LearnedParameters[p], 2
                )

        if len(to_remove) >= len(params):
            self.log_print(
                [
                    "Attempted champion reduction failed due to",
                    "all params found neglibible.",
                    "Check method of determining negligibility.",
                    "(By default, parameter removed if sigma of that",
                    "parameters final posterior > parameter.",
                    "i.e. 0 within 1 sigma of distriubtion"
                ]
            )
            return
        if len(to_remove) > 0:
            new_model_terms = list(
                set(params) - set(to_remove)
            )
            dim = database_framework.get_num_qubits(new_model_terms[0])
            p_str = 'P' * dim
            new_mod = p_str.join(new_model_terms)
            new_mod = database_framework.alph(new_mod)

            self.log_print(
                [
                    "Some neglibible parameters found:", removed_params,
                    "\nReduced champion model suggested:", new_mod
                ]
            )

            reduced_mod_info = self.add_model_to_database(
                model=new_mod,
                force_create_model=True
            )
            reduced_mod_id = reduced_mod_info['model_id']
            reduced_mod_instance = self.get_model_storage_instance_by_id(
                reduced_mod_id
            )

            reduced_mod_terms = sorted(
                database_framework.get_constituent_names_from_name(
                    new_mod
                )
            )

            # champ_attributes = list(champ_mod.__dict__.keys())
            # Here we need to fill in learned_info dict on redis with required attributes
            # fill in rds_dbs['learned_models_info'][reduced_mod_id]
            # for att in champ_attributes:
            #     reduced_mod_instance.__setattr__(
            #         att,
            #         champ_mod.__getattribute__(att)
            #     )

            # get champion leared info
            reduced_champion_info = pickle.loads(
                self.redis_databases['learned_models_info'].get(
                    str(self.ChampID))
            )

            reduced_params = {}
            reduced_sigmas = {}
            for term in reduced_mod_terms:
                reduced_params[term] = champ_mod.LearnedParameters[term]
                reduced_sigmas[term] = champ_mod.FinalSigmas[term]

            learned_params = [reduced_params[t] for t in reduced_mod_terms]
            sigmas = np.array([reduced_sigmas[t] for t in reduced_mod_terms])
            final_params = np.array(list(zip(learned_params, sigmas)))

            new_cov_mat = np.diag(
                sigmas**2
            )
            new_prior = qinfer.MultivariateNormalDistribution(
                learned_params,
                new_cov_mat
            )

            # reduce learned info where appropriate
            reduced_champion_info['name'] = new_mod
            reduced_champion_info['sim_op_names'] = reduced_mod_terms
            reduced_champion_info['final_cov_mat'] = new_cov_mat
            reduced_champion_info['final_params'] = final_params
            reduced_champion_info['learned_parameters'] = reduced_params
            reduced_champion_info['model_id'] = reduced_mod_id
            reduced_champion_info['final_prior'] = new_prior
            reduced_champion_info['est_mean'] = np.array(learned_params)
            reduced_champion_info['final_sigmas'] = reduced_sigmas
            reduced_champion_info['initial_params'] = reduced_sigmas

            compressed_reduced_champ_info = pickle.dumps(
                reduced_champion_info,
                protocol=2
            )

            # TODO fill in values for ModelInstanceForStorage
            self.redis_databases['learned_models_info'].set(
                str(float(reduced_mod_id)),
                compressed_reduced_champ_info
            )

            self.get_model_storage_instance_by_id(
                reduced_mod_id).updateLearnedValues()

            bayes_factor = self.get_pairwise_bayes_factor(
                model_a_id=int(self.ChampID),
                model_b_id=int(reduced_mod_id),
                wait_on_result=True
            )
            self.log_print(
                [
                    "[QMD] BF b/w champ and reduced champ models:",
                    bayes_factor
                ]
            )

            if (
                (
                    bayes_factor
                    < (1.0 / self.growth_class.reduce_champ_bayes_factor_threshold)
                )
                # or True
            ):
                # overwrite champ id etc

                self.log_print(
                    [
                        "Replacing champion model ({}) with reduced champion model ({} - {})".format(
                            self.ChampID,
                            reduced_mod_id,
                            new_mod
                        ),
                        "\n i.e. removing negligible parameter terms:\n{}".format(
                            removed_params
                        )

                    ]
                )
                original_champ_id = self.ChampID
                self.ChampID = reduced_mod_id
                self.ChampionName = new_mod

                self.get_model_storage_instance_by_id(self.ChampID).BayesFactors = (
                    self.get_model_storage_instance_by_id(
                        original_champ_id).BayesFactors
                )

            # TODO check if BF > threshold; if so, reassign self.ChampID and
            # self.ChampionName

        else:
            self.log_print(
                [
                    "Parameters non-negligible; not replacing champion model."
                ]
            )

    ##########
    # Section: Run available algorithms
    ##########

    def run_quantum_hamiltonian_learning(self):

        if (
            self.qhl_mode == True
            and
            self.true_model_name not in list(self.ModelsBranches.keys())
        ):
            self.new_branch(
                growth_rule=self.growth_rule_of_true_model,
                model_list=[self.true_model_name]
            )

        mod_to_learn = self.true_model_name
        self.log_print(
            [
                "QHL test on:", mod_to_learn
            ]
        )

        self.learn_model(
            model_name=mod_to_learn,
            use_rq=self.use_rq,
            blocking=True
        )

        mod_id = database_framework.model_id_from_name(
            db=self.db,
            name=mod_to_learn
        )
        self.TrueOpModelID = mod_id
        self.ChampID = mod_id
        self.log_print(
            [
                "Learned:",
                mod_to_learn,
                ". ID=",
                mod_id
            ]
        )
        mod = self.get_model_storage_instance_by_id(mod_id)
        self.log_print(["Mod (reduced) name:", mod.Name])
        mod.updateLearnedValues()

        n_qubits = database_framework.get_num_qubits(mod.Name)
        if n_qubits > 3:
            # only compute subset of points for plot
            # otherwise takes too long
            self.log_print(
                [
                    "getting new set of times to plot expectation values for"
                ]
            )
            expec_val_plot_times = self.ReducedPlotTimes
        else:
            self.log_print(
                [
                    "Using default times to plot expectation values for",
                    "num qubits:", n_qubits
                ]
            )
            expec_val_plot_times = self.PlotTimes

        self.log_print(
            [
                "times to plot for expetation values:",
                expec_val_plot_times
            ]
        )

        mod.compute_expectation_values(
            times=expec_val_plot_times,
            # plot_probe_path = self.PlotProbeFile
        )
        self.log_print(
            [
                "Finished computing expectation values for", mod.Name,
                mod.expectation_values
            ]
        )

        self.compute_f_score(
            model_id=mod_id
        )

        # TODO write single QHL test
        time_now = time.time()
        time_taken = time_now - self._start_time
#        true_model_r_squared = self.get_model_storage_instance_by_id(self.TrueOpModelID).r_squared()

        self.ResultsDict = {
            'NumParticles': self.NumParticles,
            'NumExperiments': mod.NumExperiments,
            'NumBayesTimes': self.NumTimesForBayesUpdates,
            'ResampleThreshold': self.ResampleThreshold,
            'ResamplerA': self.ResamplerA,
            'PHGPrefactor': self.PGHPrefactor,
            'ConfigLatex': self.LatexConfig,
            'Time': time_taken,
            'QID': self.qmla_id,
            'RSquaredTrueModel': mod.r_squared(
                times=expec_val_plot_times,
                plot_probes=self.PlotProbes
            ),
            'QuadraticLosses': mod.QuadraticLosses,
            'NameAlphabetical': database_framework.alph(mod.Name),
            'LearnedParameters': mod.LearnedParameters,
            'FinalSigmas': mod.FinalSigmas,
            'TrackParameterEstimates': mod.TrackParameterEstimates,
            'TrackVolume': mod.VolumeList,
            'TrackTimesLearned': mod.Times,
            # 'TrackCovarianceMatrices' : mod.TrackCovMatrices,
            'ExpectationValues': mod.expectation_values,
            # 'RSquaredByEpoch' : mod.r_squared_by_epoch(
            #     times = expec_val_plot_times,
            #     plot_probes = self.PlotProbes
            # ), # TODO only used for AnalyseMultipleQMD/r_squared_average() -- not currently in use
            # 'FinalRSquared' : mod.final_r_squared,
            # 'FinalRSquared' : mod.r_squared(
            #     plot_probes = self.PlotProbes,
            #     times = expec_val_plot_times
            # ),
            'FinalRSquared': mod.final_r_squared,
            'Fscore': self.FScore,
            'Precision': self.Precision,
            'Sensitivity': self.Sensitivity,
            'p-value': mod.p_value,
            'LearnedHamiltonian': mod.LearnedHamiltonian,
            'GrowthGenerator': mod.growth_rule_of_true_model,
            'Heuristic': mod.HeuristicType,
            'ChampLatex': mod.LatexTerm,
        }

        self.log_print(
            [
                "Stored results dict. Finished testQHL function"
            ]
        )

    def run_quantum_hamiltonian_learning_multiple_models(self, model_names=None):
        if model_names is None:
            # TODO get from growth rule
            self.log_print(
                [
                    "Multiple model QHL; model_names is None; getting initial models"
                ]
            )
            model_names = self.growth_rule.qhl_models

        current_models = list(
            self.ModelsBranches.keys()
        )
        self.log_print(
            [
                "Model Names for multiple QHL:", model_names,
                "current models:", current_models
            ]
        )
        models_to_add = []
        for mod in model_names:
            if mod not in current_models:
                models_to_add.append(mod)
        if len(models_to_add) > 0:
            self.new_branch(
                growth_rule=self.growth_rule_of_true_model,
                model_list=models_to_add
            )
        self.qhl_mode_multiple_models = True
        self.ChampID = -1,  # TODO just so not to crash during dynamics plot
        self.multiQHL_model_ids = [
            database_framework.model_id_from_name(
                db=self.db,
                name=mod_name
            ) for mod_name in model_names
        ]

        self.log_print(
            [
                'run multiple QHL. names:', model_names,
                "model ids:", self.multiQHL_model_ids
            ]
        )

        learned_models_ids = self.redis_databases['learned_models_ids']

        for mod_name in model_names:
            print("Trying to get mod id for", mod_name)
            mod_id = database_framework.model_id_from_name(
                db=self.db,
                name=mod_name
            )
            learned_models_ids.set(
                str(mod_id), 0
            )
            self.learn_model(
                model_name=mod_name,
                use_rq=self.use_rq,
                blocking=False
            )

        running_models = learned_models_ids.keys()
        self.log_print(
            [
                'Running Models:', running_models,
            ]
        )
        for k in running_models:
            while int(learned_models_ids.get(k)) != 1:
                sleep(0.01)
                self.inspect_remote_job_crashes()

        self.log_print(
            [
                'Finished waiting on queue, for all:', running_models,
            ]
        )
        time_now = time.time()
        time_taken = time_now - self._start_time
        for mod_name in model_names:
            mod_id = database_framework.model_id_from_name(
                db=self.db, name=mod_name
            )
            mod = self.get_model_storage_instance_by_id(mod_id)
            mod.updateLearnedValues(
                fitness_parameters=self.FitnessParameters
            )
            self.compute_f_score(
                model_id=mod_id
            )

            n_qubits = database_framework.get_num_qubits(mod.Name)
            if n_qubits > 5:
                # only compute subset of points for plot
                # otherwise takes too long
                self.log_print(
                    [
                        "getting new set of times to plot expectation values for"
                    ]
                )
                expec_val_plot_times = self.ReducedPlotTimes
            else:
                self.log_print(
                    [
                        "Using default times to plot expectation values for",
                        "num qubits:", n_qubits
                    ]
                )
                expec_val_plot_times = self.PlotTimes

            mod.compute_expectation_values(
                times=expec_val_plot_times,
                # plot_probe_path = self.PlotProbeFile
            )
            # equivalent to self.ResultsDict
            mod.results_dict = {
                'NumParticles': mod.NumParticles,
                'NumExperiments': mod.NumExperiments,
                'NumBayesTimes': self.NumTimesForBayesUpdates,
                'ResampleThreshold': self.ResampleThreshold,
                'ResamplerA': self.ResamplerA,
                'PHGPrefactor': self.PGHPrefactor,
                'ConfigLatex': self.LatexConfig,
                'Time': time_taken,
                'QID': self.qmla_id,
                'ChampID': self.ChampID,
                'QuadraticLosses': mod.QuadraticLosses,
                'RSquaredTrueModel': mod.r_squared(
                    plot_probes=self.PlotProbes,
                    times=expec_val_plot_times
                ),
                'NameAlphabetical': database_framework.alph(mod.Name),
                'LearnedParameters': mod.LearnedParameters,
                'FinalSigmas': mod.FinalSigmas,
                'TrackParameterEstimates': mod.TrackParameterEstimates,
                'TrackVolume': mod.VolumeList,
                'TrackTimesLearned': mod.Times,
                # 'TrackCovarianceMatrices' : mod.TrackCovMatrices,
                'ExpectationValues': mod.expectation_values,
                # 'RSquaredByEpoch' : mod.r_squared_by_epoch(
                #     times = expec_val_plot_times,
                #     plot_probes = self.PlotProbes
                # ),
                # 'FinalRSquared' : mod.final_r_squared,
                'FinalRSquared': mod.r_squared(
                    plot_probes=self.PlotProbes,
                    times=expec_val_plot_times
                ),
                'p-value': mod.p_value,
                'Fscore': self.FScore,
                'Precision': self.Precision,
                'Sensitivity': self.Sensitivity,
                'LearnedHamiltonian': mod.LearnedHamiltonian,
                'GrowthGenerator': mod.growth_rule_of_true_model,
                'Heuristic': mod.HeuristicType,
                'ChampLatex': mod.LatexTerm
            }
            self.ModelIDNames = {}
            for k in self.model_name_id_map:
                v = self.model_name_id_map[k]
                self.ModelIDNames[v] = k


    def run_complete_qmla(
        self,
        num_exp=40,
        num_spawns=1,
        max_branches=None,
        # max_num_qubits=None,
        # max_num_models=None,
        spawn=True,
        just_given_models=False
    ):

        # print("[QMD runMult] start")
        active_branches_learning_models = (
            self.redis_databases['active_branches_learning_models']
        )
        active_branches_bayes = self.redis_databases['active_branches_bayes']

        print("[QMD] Going to learn initial models from branches.")

        if self.NumTrees > 1:
            for i in list(self.BranchModels.keys()):
                # print("[QMD runMult] launching branch ", i)
                # ie initial branches
                self.learn_models_on_given_branch(
                    i,
                    blocking=False,
                    use_rq=True
                )
                while(
                    int(active_branches_learning_models.get(i))
                        < self.NumModelsPerBranch[i]
                ):
                    # don't do comparisons till all models on this branch are
                    # done
                    sleep(0.1)
                    # print("num models learned on br", i,
                    #     ":", int(active_branches_learning_models[i])
                    # )
                self.BranchAllModelsLearned[i] = True
                self.get_bayes_factors_by_branch_id(i)
                while (
                    int(active_branches_bayes.get(i))
                        < self.NumModelPairsPerBranch[i]
                ):  # bayes comparisons not done
                    # print(
                    #     "num comparisons complete br", i,
                    #     ":", int(active_branches_bayes[i]),
                    #     "should be",
                    #     self.NumModelPairsPerBranch[i]
                    # )
                    sleep(0.1)
                # self.BranchComparisonsComplete[i] = True
                self.log_print(
                    [
                        "Models computed and compared for branch",
                        i
                    ]
                )
        else:
            for i in list(self.BranchModels.keys()):
                # print("[QMD runMult] launching branch ", i)
                # ie initial branches
                self.learn_models_on_given_branch(
                    i,
                    blocking=False,
                    use_rq=True
                )

        max_spawn_depth_reached = False
        all_comparisons_complete = False

        branch_ids_on_db = list(
            active_branches_learning_models.keys()
        )
        self.log_print(
            [
                "Entering while loop of spawning/comparing."
            ]
        )
        # while max_spawn_depth_reached==False:
        while self.NumTreesCompleted < self.NumTrees:
            branch_ids_on_db = list(
                active_branches_learning_models.keys()
            )

            # print("[QMD] branches:", branch_ids_on_db)
            self.inspect_remote_job_crashes()

            for branchID_bytes in branch_ids_on_db:
                branchID = int(branchID_bytes)
                # print(
                #     "\n\tactive learning:",
                #     active_branches_learning_models.get(branchID),
                #     "\n\tnum mods on branch",
                #     self.NumModelsPerBranch[branchID],
                #     "\n\tall learned:",
                #     self.BranchAllModelsLearned[branchID]
                # )

                # print("[QMD] considering branch:", branchID)
                if (
                    int(
                        active_branches_learning_models.get(
                            branchID)
                    ) == self.NumModelsPerBranch[branchID]
                    and
                    self.BranchAllModelsLearned[branchID] == False
                ):
                    self.log_print([
                        "All models on branch",
                        branchID,
                        "have finished learning."]
                    )
                    self.BranchAllModelsLearned[branchID] = True
                    models_this_branch = self.BranchModelIds[branchID]
                    for mod_id in models_this_branch:
                        mod = self.get_model_storage_instance_by_id(mod_id)
                        mod.updateLearnedValues()

                    self.get_bayes_factors_by_branch_id(branchID)

            for branchID_bytes in active_branches_bayes.keys():

                branchID = int(branchID_bytes)
                bayes_calculated = active_branches_bayes.get(
                    branchID_bytes
                )

                if (int(bayes_calculated) ==
                        self.NumModelPairsPerBranch[branchID]
                            and
                        self.BranchComparisonsComplete[branchID] == False
                        ):
                    self.BranchComparisonsComplete[branchID] = True
                    self.compare_all_models_in_branch(branchID)
                    # print("[QMD] getting growth rule for branchID", branchID)
                    # print("[QMD] dict:", self.Branchget_growth_rule)
                    this_branch_growth_rule = self.Branchget_growth_rule[branchID]
                    if self.TreesCompleted[this_branch_growth_rule] == False:
                        print(
                            "not finished tree for growth:",
                            this_branch_growth_rule
                        )

                        growth_rule_tree_complete = self.spawn_from_branch(
                            # will return True if this brings it to
                            # self.MaxSpawnDepth
                            branchID=branchID,
                            growth_rule=this_branch_growth_rule,
                            num_models=1
                        )

                        if (
                            growth_rule_tree_complete == True
                        ):
                            self.TreesCompleted[this_branch_growth_rule] = True
                            self.NumTreesCompleted += 1
                            print(
                                "[QMD] Num trees now completed:",
                                self.NumTreesCompleted,
                                "Tree completed dict:",
                                self.TreesCompleted
                            )
                            max_spawn_depth_reached = True
                    else:
                        print(
                            "\n\n\nFinished tree for growth:",
                            this_branch_growth_rule
                        )

        self.log_print(
            [
                "All trees have completed.",
                "Num complete:",
                self.NumTreesCompleted
            ]
        )
        # let any branches which have just started finish before moving to
        # analysis
        still_learning = True

        while still_learning:
            branch_ids_on_db = list(active_branches_learning_models.keys())
            # branch_ids_on_db.remove(b'LOCKED')
            for branchID_bytes in branch_ids_on_db:
                branchID = int(branchID_bytes)
                if (
                    (int(active_branches_learning_models.get(branchID)) ==
                     self.NumModelsPerBranch[branchID])
                    and
                    (self.BranchAllModelsLearned[branchID] == False)
                ):
                    self.BranchAllModelsLearned[branchID] = True
                    self.get_bayes_factors_by_branch_id(branchID)

                if branchID_bytes in active_branches_bayes:
                    num_bayes_done_on_branch = (
                        active_branches_bayes.get(branchID_bytes)
                    )
                    # print(
                    #     "branch", branchID,
                    #     "num complete:", num_bayes_done_on_branch
                    # )
                    if (int(num_bayes_done_on_branch) ==
                            self.NumModelPairsPerBranch[branchID] and
                            self.BranchComparisonsComplete[branchID] == False
                        ):
                        self.BranchComparisonsComplete[branchID] = True
                        self.compare_all_models_in_branch(branchID)

            if (
                np.all(
                    np.array(list(self.BranchAllModelsLearned.values()))
                    == True
                )
                and
                np.all(np.array(list(
                    self.BranchComparisonsComplete.values())) == True
                )
            ):
                still_learning = False  # i.e. break out of this while loop

        print("[QMD runRemoteMult] Finalising QMD.")
        final_winner, final_branch_winners = self.perform_final_bayes_comparisons()
        self.ChampionName = final_winner
        self.ChampID = self.get_model_data_by_field(
            name=final_winner,
            field='ModelID'
        )

        # Check if final winner has parameters close to 0; potentially change
        # champ
        self.update_database_model_info()

        if (
            self.growth_class.check_champion_reducibility == True
            and
            self.growth_class.tree_completed_initially == False
        ):
            self.check_champion_reducibility()

        self.log_print(
            [
                "Final winner = ", self.ChampionName
            ]
        )

        if self.ChampionName == database_framework.alph(self.true_model_name):
            self.log_print(
                [
                    "True model found: {}".format(
                        database_framework.alph(self.true_model_name)
                    )
                ]
            )

        self.finalise_qmla()


    ##########
    # Section: Analysis/plotting functions
    ##########

    def compute_f_score(
        self,
        model_id,
        beta=1  # beta=1 for F1-score. Beta is relative importance of sensitivity to precision
    ):

        true_set = self.growth_class.true_operator_terms

        growth_class = self.get_model_storage_instance_by_id(model_id).growth_class
        terms = [
            growth_class.latex_name(
                term
            )
            for term in
            database_framework.get_constituent_names_from_name(
                self.model_name_id_map[model_id]
            )
        ]
        learned_set = set(sorted(terms))
        self.TotalPositives = len(true_set)
        self.TruePositives = len(true_set.intersection(learned_set))
        self.FalsePositives = len(learned_set - true_set)
        self.false_negatives = len(true_set - learned_set)
        self.Precision = self.TruePositives / \
            (self.TruePositives + self.FalsePositives)
        self.Sensitivity = self.TruePositives / self.TotalPositives
        try:
            self.FScore = (
                (1 + beta**2) * (
                    (self.Precision * self.Sensitivity)
                    / (beta**2 * self.Precision + self.Sensitivity)
                )
            )
        except BaseException:
            # both precision and sensitivity=0 as true_positives=0
            self.FScore = 0

        return self.FScore

    def plot_branch_champs_quadratic_losses(
        self,
        save_to_file=None,
    ):
        qmla.analysis.plot_quadratic_loss(
            qmd=self,
            champs_or_all='champs',
            save_to_file=save_to_file
        )

    def plot_branch_champs_volumes(self, model_id_list=None, branch_champions=False,
                    branch_id=None, save_to_file=None
                    ):

        plt.clf()
        plot_descriptor = '\n(' + str(self.NumParticles) + 'particles; ' + \
            str(self.NumExperiments) + 'experiments).'

        if branch_champions:
            # only plot for branch champions
            model_id_list = list(self.BranchChampions.values())
            plot_descriptor += '[Branch champions]'

        elif branch_id is not None:
            model_id_list = database_framework.list_model_id_in_branch(
                self.db, branch_id)
            plot_descriptor += '[Branch' + str(branch_id) + ']'

        elif model_id_list is None:
            self.log_print(["Plotting volumes for all models by default."])

            model_id_list = range(self.HighestModelID)
            plot_descriptor += '[All models]'

        plt.title('Volume evolution through QMD ' + plot_descriptor)
        plt.xlabel('Epoch')
        plt.ylabel('Volume')

        for i in model_id_list:
            vols = self.get_model_storage_instance_by_id(i).VolumeList
            plt.semilogy(vols, label=str('ID:' + str(i)))
#            plt.legend(loc='upper right', bbox_to_anchor=(1.3, 1.0))

        ax = plt.subplot(111)

        # Shrink current axis's height by 10% on the bottom
        box = ax.get_position()
        ax.set_position([box.x0, box.y0 + box.height * 0.1,
                         box.width, box.height * 0.9])

        # Put a legend below current axis
        lgd = ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15),
                        fancybox=True, shadow=True, ncol=4)

        if save_to_file is None:
            plt.show()
        else:
            plt.savefig(
                save_to_file, bbox_extra_artists=(
                    lgd,), bbox_inches='tight')

    def store_bayes_factors_to_csv(self, save_to_file, names_ids='latex'):
        qmla.analysis.BayesFactorsCSV(self, save_to_file, names_ids=names_ids)

    def store_bayes_factors_to_shared_csv(self, bayes_csv):
        print("[QMD] writing Bayes CSV")
        qmla.analysis.updateAllBayesCSV(self, bayes_csv)

    def plot_parameter_learning_single_model(
        self,
        model_id=0,
        true_model=False,
        save_to_file=None
    ):

        if true_model:
            model_id = database_framework.model_id_from_name(
                db=self.db, name=self.true_model_name)

        qmla.analysis.parameterEstimates(qmd=self,
                                   modelID=model_id,
                                   use_experimental_data=self.use_experimental_data,
                                   save_to_file=save_to_file
                                   )

    def plot_branch_champions_dynamics(
        self,
        all_models=False,
        model_ids=None,
        include_bayes_factors_in_dynamics_plots=True,
        include_param_estimates_in_dynamics_plots=False,
        include_times_learned_in_dynamics_plots=True,
        save_to_file=None,
    ):
        if all_models == True:
            model_ids = list(sorted(self.model_name_id_map.keys()))
        elif model_ids is None:
            model_ids = list(
                sorted(self.BranchChampions.values())
            )

        qmla.analysis.plotDynamicsLearnedModels(
            qmd=self,
            include_bayes_factors=include_bayes_factors_in_dynamics_plots,
            include_times_learned=include_times_learned_in_dynamics_plots,
            include_param_estimates=include_param_estimates_in_dynamics_plots,
            model_ids=model_ids,
            save_to_file=save_to_file,
        )

    def plot_volume_after_qhl(self,
                      model_id=None,
                      true_model=True,
                      show_resamplings=True,
                      save_to_file=None
                      ):
        qmla.analysis.plotVolumeQHL(
            qmd=self,
            model_id=model_id,
            true_model=true_model,
            show_resamplings=show_resamplings,
            save_to_file=save_to_file
        )

    def plot_qmla_tree(
        self,
        modlist=None,
        only_adjacent_branches=True,
        save_to_file=None
    ):
        qmla.analysis.plotQMDTree(
            self,
            modlist=modlist,
            only_adjacent_branches=only_adjacent_branches,
            save_to_file=save_to_file
        )

    def plot_qmla_radar_scores(self, modlist=None, save_to_file=None):
        plot_title = str("Radar Plot QMD " + str(self.qmla_id))
        if modlist is None:
            modlist = list(self.BranchChampions.values())
        qmla.analysis.plotRadar(
            self,
            modlist,
            save_to_file=save_to_file,
            plot_title=plot_title
        )

    def plot_r_squared_by_epoch_for_model_list(
        self,
        modlist=None,
        save_to_file=None
    ):
        if modlist is None:
            modlist = []
            try:
                modlist.append(self.ChampID)
            except BaseException:
                pass
            try:
                modlist.append(self.TrueOpModelID)
            except BaseException:
                pass

        qmla.analysis.r_squared_from_epoch_list(
            qmd=self,
            model_ids=modlist,
            save_to_file=save_to_file
        )

    def one_qubit_probes_bloch_sphere(self):
        print("In jupyter, include the following to view sphere: %matplotlib inline")
        # import qutip as qt
        bloch = qt.Bloch()
        for i in range(self.NumProbes):
            state = self.ProbeDict[i, 1]
            a = state[0]
            b = state[1]
            A = a * qt.basis(2, 0)
            B = b * qt.basis(2, 1)
            vec = (A + B)
            print(vec)
            bloch.add_states(vec)
        bloch.show()


##########
# Section: Miscellaneous functions called within QMLA
##########


def num_pairs_in_list(num_models):
    if num_models <= 1:
        return 0

    n = num_models
    k = 2  # ie. nCk where k=2 since we want pairs

    try:
        a = math.factorial(n) / math.factorial(k)
        b = math.factorial(n - k)
    except BaseException:
        print("Numbers too large to compute number pairs. n=", n, "\t k=", k)

    return a / b
