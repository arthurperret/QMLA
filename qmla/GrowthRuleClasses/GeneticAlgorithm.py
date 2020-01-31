import numpy as np
import itertools
import sys
import os
import random
import copy
import scipy
import time

import qmla.DataBase as DataBase
import qmla.ModelNames as ModelNames
import qmla.model_generation as model_generation


class GeneticAlgorithmQMLA():
    def __init__(
        self,
        num_sites,
        base_terms=['x', 'y', 'z'],
        mutation_probability=0.1,
        log_file=None, 
    ):
        self.num_sites = num_sites
        self.base_terms = base_terms
        self.get_base_chromosome()
#         self.addition_str = 'P'*self.num_sites
        self.addition_str = '+'
        self.mutation_probability = mutation_probability
        self.previously_considered_chromosomes = []
        self.log_file = log_file
        self.chromosomes_at_generation = {}
        self.genetic_generation = 0


    def get_base_chromosome(self):
        """
        get empty chromosome with binary
        position for each possible term
        within this model type

        Basic: all pairs can be connected operator o on sites i,j:
        e.g. i=4,j=7,N=9: IIIoIIoII
        """

        basic_chromosome = []
        chromosome_description = []
        for i in range(1, 1 + self.num_sites):
            for j in range(i + 1, 1 + self.num_sites):
                for t in self.base_terms:
                    pair = (int(i), int(j), t)
                    pair = tuple(pair)
                    basic_chromosome.append(0)
                    chromosome_description.append(pair)

        self.chromosome_description = chromosome_description
        self.chromosome_description_array = np.array(
            self.chromosome_description)
        self.basic_chromosome = np.array(basic_chromosome)
        self.num_terms = len(self.basic_chromosome)
        # print("Chromosome definition:", self.chromosome_description_array)
#         binary_combinations = list(itertools.product([0,1], repeat=self.num_terms))
#         binary_combinations = [list(b) for b in binary_combinations]
#         self.possible_chromosomes = np.array(binary_combinations)

    def map_chromosome_to_model(
        self,
        chromosome,
    ):
        if isinstance(chromosome, str):
            chromosome = list(chromosome)
            chromosome = np.array([int(i) for i in chromosome])

        nonzero_postions = chromosome.nonzero()
        present_terms = list(
            self.chromosome_description_array[nonzero_postions]
        )
        term_list = []
        for t in present_terms:
            i = t[0]
            j = t[1]
            o = t[2]

            term = 'pauliSet_{i}J{j}_{o}J{o}_d{N}'.format(
                i=i,
                j=j,
                o=o,
                N=self.num_sites
            )
            term_list.append(term)

        model_string = self.addition_str.join(term_list)
        # print(
        #     "[GeneticAlgorithm mapping chromosome to model] \
        #     \n chromosome: {} \
        #     \n model string: {}\
        #     \n nonzero_postions: {}".format(
        #     chromosome,
        #     model_string,
        #     nonzero_postions
        #     )
        # )

        return model_string

    def map_model_to_chromosome(
        self,
        model
    ):
        terms = DataBase.get_constituent_names_from_name(model)
        chromosome_locations = []
        for term in terms:
            components = term.split('_')
            try:
                components.remove('pauliSet')
            except BaseException:
                print(
                    "[GA - map model to chromosome] \
                    \nCannot remove pauliSet from components:",
                    components,
                    "\nModel:", model
                )
                raise
            core_operators = list(sorted(DataBase.core_operator_dict.keys()))
            for l in components:
                if l[0] == 'd':
                    dim = int(l.replace('d', ''))
                elif l[0] in core_operators:
                    operators = l.split('J')
                else:
                    sites = l.split('J')
            # get strings when splitting the list elements
            sites = [int(s) for s in sites]
            sites = sorted(sites)

            term_desc = [sites[0], sites[1], operators[0]]
            term_desc = tuple(term_desc)
            term_chromosome_location = self.chromosome_description.index(
                term_desc)
            chromosome_locations.append(term_chromosome_location)
        new_chromosome = copy.copy(self.basic_chromosome)
        new_chromosome[chromosome_locations] = 1
        return new_chromosome

    def chromosome_string(
        self,
        c
    ):
        b = [str(i) for i in c]
        return ''.join(b)

    def random_initial_models(
        self,
        num_models=5
    ):
        new_models = []
        self.chromosomes_at_generation[0] = []

        while len(new_models) < num_models:
            r = random.randint(1, 2**self.num_terms)
            r = format(r, '0{}b'.format(self.num_terms))

            if self.chromosome_string(
                    r) not in self.previously_considered_chromosomes:
                r = list(r)
                r = np.array([int(i) for i in r])
                mod = self.map_chromosome_to_model(r)

                self.previously_considered_chromosomes.append(
                    self.chromosome_string(r)
                )
                self.chromosomes_at_generation[0].append(
                    self.chromosome_string(r)
                )

                new_models.append(mod)

        # new_models = list(set(new_models))
        # print("Random initial models:", self.previously_considered_chromosomes)
        # print("Random initial models:", new_models)
        return new_models

    def selection(
        self,
        model_fitnesses,
        num_chromosomes_to_select=2,
        num_pairs_to_sample=5,
        **kwargs
    ):
        models = list(model_fitnesses.keys())
        num_nonzero_fitness_models = np.count_nonzero(
            list(model_fitnesses.values()))
        num_models = len(models)

        max_possible_num_combinations = scipy.misc.comb(
            num_nonzero_fitness_models, 2)

        self.log_print(
            [
                "[Selection] Getting max possible combinations: {} choose {} = {}".format(
                num_nonzero_fitness_models, 2, max_possible_num_combinations
                )
            ]        
        )

        num_pairs_to_sample = min(
            num_pairs_to_sample,
            max_possible_num_combinations
        )

        chromosome_fitness = {}
        chromosomes = {}
        weights = []
        self.log_print(
            [
            "Getting weights of input models."
            ]
        )
        for model in models:
            self.log_print(
                [
                    "Mapping {} to chromosome".format(model) 
                ]
            )
            chrom = self.map_model_to_chromosome(model)
            chromosomes[model] = chrom
            weights.append(model_fitnesses[model])
        weights /= np.sum(weights)  # normalise so weights are probabilities
        self.log_print(
            [
            "Models: {} \nWeights: {}".format(models, weights)
            ]
        )
        new_chromosome_pairs = []
        combinations = []

        while len(new_chromosome_pairs) < num_pairs_to_sample:
            # TODO: better way to sample multiple pairs
            selected_models = np.random.choice(
                models,
                size=num_chromosomes_to_select,
                p=weights,
                replace=False
            )
            selected_chromosomes = [
                chromosomes[mod] for mod in selected_models
            ]
            combination = ''.join(
                [
                    str(i) for i in list(selected_chromosomes[0] + selected_chromosomes[1])
                ]
            )
            # print("Trying combination {}".format(combination))
            if combination not in combinations:
                combinations.append(combination)
                new_chromosome_pairs.append(selected_chromosomes)
                self.log_print(
                    [
                    "Including selected models:", selected_models
                    ]
                )
                self.log_print(
                    [
                    "Now {} combinations of {}".format(
                        len(new_chromosome_pairs),
                        num_pairs_to_sample
                    )
                    ]
                )
        self.log_print(
            [
            "[Selection] Returning {}".format(new_chromosome_pairs)
            ]
        )
        return new_chromosome_pairs

    def crossover(
        self,
        chromosomes,
    ):
        """
        This fnc assumes only 2 chromosomes to crossover
        and does so in the most basic method of splitting
        down the middle and swapping
        """

        c1 = copy.copy(chromosomes[0])
        c2 = copy.copy(chromosomes[1])

        x = int(len(c1) / 2)
        tmp = c2[:x].copy()
        c2[:x], c1[:x] = c1[:x], tmp

        return c1, c2

    def mutation(
        self,
        chromosomes,
    ):
        copy_chromosomes = copy.copy(chromosomes)
        for c in copy_chromosomes:
            if np.all(c == 0):
                print(
                    "Input chomosome {} has no interactions -- forcing mutation".format(
                        c)
                )
                mutation_probability = 1.0
            else:
                mutation_probability = self.mutation_probability

            if np.random.rand() < mutation_probability:
                idx = np.random.choice(range(len(c)))
                # print("Flipping idx {}".format(idx))
                if c[idx] == 0:
                    c[idx] = 1
                elif c[idx] == 1:
                    c[idx] = 0
        return chromosomes

    def genetic_algorithm_step(
        self,
        model_fitnesses,
        num_pairs_to_sample=5
    ):
        new_models = []
        chromosomes_selected = self.selection(
            model_fitnesses=model_fitnesses,
            num_pairs_to_sample=num_pairs_to_sample
        )
        new_chromosomes_this_generation = []
        for chromosomes in chromosomes_selected:
            new_chromosomes = self.crossover(chromosomes)
            new_chromosomes = self.mutation(new_chromosomes)
            new_chromosomes_this_generation.extend(new_chromosomes)

            new_models.extend(
                [
                    self.map_chromosome_to_model(c)
                    for c in new_chromosomes
                ]
            )

        self.previously_considered_chromosomes.extend([
            self.chromosome_string(r) for r in new_chromosomes_this_generation
            ]
        )
        self.genetic_generation += 1
        self.chromosomes_at_generation[self.genetic_generation] = [
            self.chromosome_string(r) for r in new_chromosomes_this_generation
        ]
        return new_models

    def log_print(
        self,
        to_print_list
    ):
        identifier = "[Genetic algorithm]"
        if type(to_print_list) != list:
            to_print_list = list(to_print_list)

        print_strings = [str(s) for s in to_print_list]
        to_print = " ".join(print_strings)
        with open(self.log_file, 'a') as write_log_file:
            print(
                identifier,
                str(to_print),
                file=write_log_file,
                flush=True
            )
