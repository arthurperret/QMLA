"""
This library provides a database framework for QMD/QML.

A Pandas dataframe is used as a database for running QMD, recording:
  - model name (and name reordered alphabetically)
  - Log Likelihood
  - Origin epoch
  - QML Class
  - Qubits acted on: which qubits have some operator acting on
  ### Note: qubit count starts at 1 -- should it start from 0??
  - Root Node
  - Selected
  - Status

A separate database holds all information on individual models:
  - constituent operators (names and matrices)
        [i.e. those which are summed to give model]
  - total matrix
  - number of qubits (dimension)

The database is generated by the function launch_db. E.g. usage:

  $ db, model_db, model_lists = database_framework.launch_db(gen_list=gen_list)

This returns:

  - db: "running database", info on dlog likelihood, etc.
  - model_db: info on construction of model, i.e. constituent operators etc.
  - model_lists = list of lists containing alphabetised model names.
        When a new model is considered, it should be compared against models of
        identical dimension (number of qubits) by alhpabetical name. If the
        alphabetical name is found in, e.g. model_lists[3], it has already been
        considered and the QML should be terminated.


To fill the data base, a list of generators are passed to launch_db.
These are strings corresponding to unique models, e.g. 'xTy' means pauli_x TENSOR_PROD pauli_y
(see Naming_Convention.pdf).
These names are used to generate instances of the operator class (defined here).
This class computes, based on the name, what the constituent operator names, matrices, total matrix, etc.
of the given model are, and fills these values into the model_db.

e.g. usage of operator:
  $ name='xPyTz'
  $ test_op = Operator(name)
  $ print(test_op.name)
  $ print(test_op.matrix)
  $ print(test_op.constituents_operators

"""

from __future__ import print_function  # so print doesn't show brackets

import numpy as np
import itertools as itr
import copy
import os as os
import sys as sys
import pandas as pd
import warnings
import hashlib

import redis
# from qinfer import NormalDistribution




__all__ = [
    'core_operator_dict', 
    'Operator',
    'get_num_qubits', 
    'get_constituent_names_from_name', 
    'alph', 
    # 'process_basic_operator', 
    'consider_new_model', 
    'reduced_model_instance_from_id',
    'update_field', 
    'pull_field', 
    'check_model_exists',
    'unique_model_pair_identifier',
    'all_active_model_ids',
    'model_id_from_name',
    'list_model_id_in_branch'
]

def identity():
    return np.array([[1 + 0.j, 0 + 0.j], [0 + 0.j, 1 + 0.j]])


def sigmaz():
    return np.array([[1 + 0.j, 0 + 0.j], [0 + 0.j, -1 + 0.j]])


def sigmax():
    return np.array([[0 + 0.j, 1 + 0.j], [1 + 0.j, 0 + 0.j]])


def sigmay():
    return np.array([[0 + 0.j, 0 - 1.j], [0 + 1.j, 0 + 0.j]])


def addition():
    return np.array([[0 + 0.j, 1 + 0.j], [0 + 0.j, 0 + 0.j]])


def subtraction():
    return np.array([[0 + 0.j, 0 + 0.j], [1 + 0.j, 0 + 0.j]])


# global core_operator_dict
core_operator_dict = {
    'i': identity(),
    'x': sigmax(),
    'y': sigmay(),
    'z': sigmaz(),
    'a': addition(),
    's': subtraction()
}

global paulis_names
paulis_names = list(core_operator_dict.keys())
core_terms_with_identity = list(core_operator_dict.keys())
core_terms_no_identity = copy.copy(
    list(core_operator_dict.keys())).remove('i')
pauli_cores_with_identity = ['x', 'y', 'z', 'i']
pauli_cores_no_identity = ['x', 'y', 'z']
plus_basis_with_identity = ['a', 's', 'i']
plus_basis_no_identity = ['a', 's']

"""
------ ------ Operator Class ------ ------
"""


def time_seconds():
    import datetime
    now = datetime.date.today()
    hour = datetime.datetime.now().hour
    minute = datetime.datetime.now().minute
    second = datetime.datetime.now().second
    time = str(str(hour) + ':' + str(minute) + ':' + str(second))
    return time


def log_print(to_print_list, log_file):
    identifier = str(str(time_seconds()) + " [DB]")
    if not isinstance(to_print_list, list):
        to_print_list = list(to_print_list)

    print_strings = [str(s) for s in to_print_list]
    to_print = " ".join(print_strings)
    with open(log_file, 'a') as write_log_file:
        print(
            identifier,
            str(to_print),
            file=write_log_file,
            flush=True
        )


class Operator():
    """
    Operator class:
    Takes one argument: name (string) according to naming convention.
    Name specifies all details of operator.
    e.g.
    - xPy is X+Y, 1 qubit
    - xTz is x TENSOR_PROD Z, 2 qubits
    - xMyTz is (X PROD Y) TENSOR_PROD Z, 2 qubits
    - xPzTiTTz is (X+Z) TENSOR_PROD I TENSOR_PROD Z
      -- 3 qubit operator. X+Z on qubit 1; I on qubit 2; Z on qubit 3
    See Naming_Convention.pdf for details.

    Constituents of an operator are operators of the same dimension
        which sum to give the operator.
    e.g.
    - xPy = X + Y has constituents X, Y

    Assigns properties for :
    - constituents_names: strings specifying constituents
    - constituents_operators: whole matrices of constituents
    - num_qubits: total dimension of operator [number of qubits it acts on]
    - matrix: total matrix operator
    - qubits_acted_on: list of qubits which are acted on non-trivially
      -- e.g. xTiTTz has list [1,3], since qubit 2 is acted on by identity
    - alph_name: rearranged version of name which follows alphabetical convention
      -- uniquely identifies equivalent operators for comparison
            against previously considered models
    -
    """

    def __init__(self, name, undimensionalised_name=None):
        self.name = name
        if undimensionalised_name is not None:
            self.undimensionalised_name = undimensionalised_name

    @property
    def constituents_names(self):
        """
        List of constituent operators names.
        """

        return get_constituent_names_from_name(self.name)

    @property
    def num_qubits(self):
        """
        Number of qubits this operator acts on.
        """
        return get_num_qubits(self.name)

    @property
    def constituents_operators(self):
        """
        List of matrices of constituents.
        """
        ops = []
        for i in self.constituents_names:
            ops.append(compute(i))
        return ops

    @property
    def num_constituents(self):
        """
        Integer, how many constituents, and therefore parameters, are in this model.
        """
        return len(self.constituents_names)

    @property
    def matrix(self):
        """
        Full matrix of operator.
        """
        mtx = None
        for i in self.constituents_operators:
            if mtx is None:
                mtx = i
            else:
                mtx += i
        return mtx

    @property
    def qubits_acted_on(self):
        """
        List of qubits which are acted on non-trivially by this operator.
        TODO: qubit count starts from 1 -- should it start from 0?
        """
        return list_used_qubits(self.name)

    @property
    def two_to_power_used_qubits_sum(self):
        """
        Binary sum of operators acted on.
        For use in comparing new operators. [Not currently used]
        """
        running_sum = 0
        for element in list_used_qubits(self.name):
            running_sum += 2**element
        return running_sum

    @property
    def alph_name(self):
        """
        Name of operator rearranged to conform with alphabetical naming convention.
        Uniquely identifies equivalent operators.
        For use when comparing potential new operators.
        """
        return alph(self.name)

    @property
    def ideal_probe(self):
        return ideal_probe(self.name)

    @property
    def eigenvectors(self):
        return get_eigenvectors(self.name)


"""
Functions for use by operator class to parse string (name) and prodcue relevent operators, lists etc.
"""


def reduced_operators(name, max_dim):
    t_str = ''
    p_str = 'P'
    for i in range(max_dim):
        t_str += 'T'
        p_str += 'P'

    split_by_t = name.split(t_str)
    reduced_dim_op = split_by_t[0]
    op = Operator(reduced_dim_op)
    return op.constituents_operators


def print_matrix(name):
    op = Operator(name)
    print(op.matrix)


def get_num_qubits(name):
    """
    Parse string and determine number of qubits this operator acts on.
    """
    # TODO this function only supports Pauli and hopping types, should be
    # generalised..
    t_str, p_str, max_t, max_p = get_t_p_strings(name)
    individual_terms = get_constituent_names_from_name(name)
    for term in individual_terms:
        if (
            term[0:1] == 'h_'
            or
            '1Dising' in term
            or
            'Heis' in term
            or
            'nv' in term
            or
            'pauliSet' in term
            or
            'transverse' in term
            or
            'FH' in term
        ):
            terms = term.split('_')
            dim_term = terms[-1]
            dim = int(dim_term[1:])
            num_qubits = dim
            return num_qubits

    # if hopping term wasn't found in individual terms
    max_t_found = 0
    t_str = ''
    while name.count(t_str + 'T') > 0:
        t_str = t_str + 'T'
    num_qubits = len(t_str) + 1

    return num_qubits

# def get_constituent_names_from_name(name):
# ### Deprecated -- now verbose_naming_mechanism_separate_terms
#     t_str, p_str, max_t, max_p = get_t_p_strings(name)
#     if(max_t >= max_p):
#         # if more T's than P's in name,
#         # it has only one constituent.
#         return [name]
#     else:
#         # More P's indicates a sum at the highest dimension.
#         return name.split(p_str)


def get_constituent_names_from_name(name):
    verbose_naming_mechanism_terms = ['T', 'P', 'M']

    if np.any(
        [t in name for t in verbose_naming_mechanism_terms]
    ):
        return verbose_naming_mechanism_separate_terms(name)
    else:
        return name.split('+')


def verbose_naming_mechanism_separate_terms(name):
    t_str, p_str, max_t, max_p = get_t_p_strings(name)
    if(max_t >= max_p):
        # if more T's than P's in name,
        # it has only one constituent.
        return [name]
    else:
        # More P's indicates a sum at the highest dimension.
        return name.split(p_str)


def list_used_qubits(name):
    """
    Parse string and determine which qubits are acted on non-trivially.
    """
    max_t, t_str = find_max_letter(name, "T")
    max_p, p_str = find_max_letter(name, "P")
    running_list = []

    if max_p >= max_t:
        list_by_p_sep = []
        if p_str == '':
            # In case of empty separator, split by anything into one string
            p_str = 'RRR'

        sep_by_p = name.split(p_str)
        for element in sep_by_p:
            list_by_p_sep.append(get_acted_on_qubits(element))

        for i in range(len(list_by_p_sep)):
            to_add = list(set(list_by_p_sep[i]) - set(running_list))
            running_list = running_list + to_add

    else:
        running_list = get_acted_on_qubits(name)
    return running_list


def get_acted_on_qubits(name):
    """
    Parse string and determine which qubits are acted on non-trivially.
    """
    max_t, t_str = find_max_letter(name, "T")
    max_p, p_str = find_max_letter(name, "P")
    if max_p > max_t:
        list_by_p_sep = []
        if p_str == '':
            # In case of empty separator, split by anything into one string
            p_str = 'RRR'

        sep_by_p = name.split(p_str)
        for element in sep_by_p:
            list_by_sep.append(fill_qubits_acted_on_list, element)

    qubits_acted_on = []
    fill_qubits_acted_on_list(qubits_acted_on, name)
    return sorted(qubits_acted_on)


def fill_qubits_acted_on_list(qubits_acted_on, name):
    """
    Parse string and determine which qubits are acted on non-trivially.
    Return list of those qubits.
    """
    max_t, t_str = find_max_letter(name, "T")
    max_p, p_str = find_max_letter(name, "P")
    if(max_p > max_t):
        string_to_analyse = name.split(p_str)[0]
    else:
        string_to_analyse = name

    if max_t == 0:
        if string_to_analyse != 'i':
            qubits_acted_on.append(1)

    else:
        i = max_t
        this_t_str = t_str
        broken_down = string_to_analyse.split(this_t_str)
        lhs = broken_down[0]
        rhs = broken_down[1]
        if rhs != 'i':
            qubits_acted_on.append(i + 1)

        if max_t == 1:
            if lhs != 'i':
                qubits_acted_on.append(1)
        else:
            fill_qubits_acted_on_list(qubits_acted_on, lhs)


def get_t_p_strings(name):
    """
    Find largest instance of consecutive P's and T's.
    Return those instances and lengths of those instances.
    """
    t_str = ''
    p_str = ''
    while name.count(t_str + 'T') > 0:
        t_str = t_str + 'T'

    while name.count(p_str + 'P') > 0:
        p_str = p_str + 'P'

    max_t = len(t_str)
    max_p = len(p_str)

    return t_str, p_str, max_t, max_p


def find_max_letter(string, letter):
    """
    Find largest instance of consecutive given 'letter'.
    Return largest instance and length of that instance.
    """
    letter_str = ''
    while string.count(letter_str + letter) > 0:
        letter_str = letter_str + letter

    return len(letter_str), letter_str


def empty_array_of_same_dim(name):
    """
    Parse name to find size of system it acts on.
    Produce an empty matrix of that dimension and return it.
    """
    # t_str=''
    # while name.count(t_str+'T')>0:
    #     t_str=t_str+'T'

    # num_qubits = len(t_str) +1
    # dim = 2**num_qubits
    num_qubits = get_num_qubits(name)
    dim = 2**num_qubits
    empty_mtx = np.zeros([dim, dim], dtype=np.complex128)
    return empty_mtx


def alph(name):
    """
    Return alphabetised version of name.
    Parse string and recursively call alph function to alphabetise substrings.
    """
    t_max, t_str = find_max_letter(name, "T")
    p_max, p_str = find_max_letter(name, "P")
    m_max, m_str = find_max_letter(name, "M")

    if p_max == 0 and t_max == 0 and p_max == 0:
        return name

    if p_max > t_max and p_max > m_max:
        ltr = 'P'
        string = p_str
    elif t_max >= p_max:
        string = t_str
        ltr = 'T'
    elif m_max >= p_max:
        string = m_str
        ltr = 'M'
    elif t_max > m_max:
        string = t_str
        ltr = 'T'
    else:
        ltr = 'M'
        string = m_str

    spread = name.split(string)
    if p_max == m_max and p_max > t_max:
        string = p_str
        list_elements = name.split(p_str)

        for i in range(len(list_elements)):
            list_elements[i] = alph(list_elements[i])
        sorted_list = sorted(list_elements)
        linked_sorted_list = p_str.join(sorted_list)
        return linked_sorted_list

    if ltr == 'P' and p_max == 1:
        sorted_spread = sorted(spread)
        out = string.join(sorted_spread)
        return out
    elif ltr == 'P' and p_max > 1:
        list_elements = name.split(string)
        sorted_list = sorted(list_elements)
        for i in range(len(sorted_list)):
            sorted_list[i] = alph(sorted_list[i])
        linked_sorted_list = string.join(sorted_list)
        return linked_sorted_list
    else:
        for i in range(len(spread)):
            spread[i] = alph(spread[i])
        out = string.join(spread)
        return out


def compute_t(inp):
    """
    Assuming largest instance of action on inp is tensor product, T.
    Parse string.
    Recursively call compute() function.
    Tensor product resulting lists.
    Return operator which is specified by inp.
    """
    max_t, t_str = find_max_letter(inp, "T")
    max_p, p_str = find_max_letter(inp, "P")

    if(max_p == 0 and max_t == 0):
        pauli_symbol = inp
        return core_operator_dict[pauli_symbol]

    elif(max_t == 0):
        return compute(inp)
    else:
        to_tens = inp.split(t_str)
        running_tens_prod = compute(to_tens[0])
        for i in range(1, len(to_tens)):
            max_p, p_str = find_max_letter(to_tens[i], "P")
            max_t, t_str = find_max_letter(to_tens[i], "T")
            rhs = compute(to_tens[i])
            running_tens_prod = np.kron(running_tens_prod, rhs)
        return running_tens_prod


def compute_p(inp):
    """
    Assuming largest instance of action on inp is addition, P.
    Parse string.
    Recursively call compute() function.
    Sum resulting lists.
    Return operator which is specified by inp.
    """
    max_p, p_str = find_max_letter(inp, "P")
    max_t, t_str = find_max_letter(inp, "T")

    if(max_p == 0 and max_t == 0):
        pauli_symbol = inp
        return core_operator_dict[pauli_symbol]

    elif max_p == 0:
        return compute(inp)
    else:
        to_add = inp.split(p_str)
        #print("To add : ", to_add)
        running_sum = empty_array_of_same_dim(to_add[0])
        for i in range(len(to_add)):
            max_p, p_str = find_max_letter(to_add[i], "P")
            max_t, t_str = find_max_letter(to_add[i], "T")

           # print("To add [i=", i, "]:", to_add[i] )
            rhs = compute(to_add[i])
            #print("SUM shape:", np.shape(running_sum))
            #print("RHS shape:", np.shape(rhs))
            running_sum += rhs

        #print("RESULT ", p_str, " : ", inp, ": \n", running_sum)
        return running_sum


def compute_m(inp):
    """
    Assuming largest instance of action on inp is multiplication, M.
    Parse string.
    Recursively call compute() function.
    Multiple resulting lists.
    Return operator which is specified by inp.
    """

    max_m, m_str = find_max_letter(inp, "M")
    max_p, p_str = find_max_letter(inp, "P")
    max_t, t_str = find_max_letter(inp, "T")

    if(max_m == 0 and max_t == 0 and max_p == 0):
        pauli_symbol = inp
        return core_operator_dict[pauli_symbol]

    elif max_m == 0:
        return compute(inp)

    else:
        to_mult = inp.split(m_str)
        #print("To mult : ", to_mult)
        t_str = ''
        while inp.count(t_str + 'T') > 0:
            t_str = t_str + 'T'

        num_qubits = len(t_str) + 1
        dim = 2**num_qubits

        running_product = np.eye(dim)

        for i in range(len(to_mult)):
            running_product = np.dot(running_product, compute(to_mult[i]))

        return running_product


def compute(inp):
    """
    Parse string.
    Recursively call compute() functions (compute_t, compute_p, compute_m).
    Tensor product, multiply or sum resulting lists.
    Return operator which is specified by inp.
    """
    from qmla.process_string_to_matrix import process_basic_operator
    max_p, p_str = find_max_letter(inp, "P")
    max_t, t_str = find_max_letter(inp, "T")
    max_m, m_str = find_max_letter(inp, "M")

    if (max_m == 0 and max_t == 0 and max_p == 0):
        basic_operator = inp
        # return core_operator_dict[basic_operator]
        return process_basic_operator(basic_operator)
    elif max_m > max_t:
        return compute_m(inp)
    elif max_t >= max_p:
        return compute_t(inp)
    else:
        return compute_p(inp)




def ideal_probe(name):
    """
    Returns a probe state which is the normalised sum of the given operators
    eigenvectors, ideal for probing that operator.
    """
    mtx = Operator(name).matrix
    eigvalues = np.linalg.eig(mtx)[1]
    summed_eigvals = np.sum(eigvalues, axis=0)
    normalised_probe = summed_eigvals / np.linalg.norm(summed_eigvals)
    return normalised_probe


def get_eigenvectors(name):
    mtx = Operator(name).matrix
    eigvectors = np.linalg.eig(mtx)[0]
    return eigvectors


"""
------ ------ Database declaration and functions ------ ------
"""

"""
Initial distribution to sample from, normal_dist
"""
# TODO: change mean and var?
# normal_dist_width = 0.25

# """
# QML parameters
# #TODO: maybe these need to be changed
# """
# n_particles = 2000
# n_experiments = 300
# #true_operator_list = [sigmax(), sigmay()]
# true_operator_list = np.array([sigmax(), sigmay()])

# #xtx = Operator('xTx')
# ytz = Operator('yTz')
# true_operator_list = np.array([ ytz.matrix] )




def get_location(db, name):
    """
    Return which row in db corresponds to the string name.
    """
#    for i in range(len(db['<Name>'])):
    for i in list(db.index.values):
        if db['<Name>'][i] == name:
            return i


def get_location_by_alph_name(db, name):
    """
    Return which row in db corresponds to the string name.
    Pass in alphabetised version of name.
    """
    location = None
#    for i in range(len(db['Alph_Name'])):
    for i in list(db.index.values):
        if db['Alph_Name'][i] == name:
            location = i
    return location


def consider_new_model(model_lists, name, db):
    """
    Check whether the new model, name, exists in all previously considered models,
    held in model_lists.
    If name has not been previously considered, 'New' is returned.
    If name has been previously considered, the corresponding location
        in db is returned.
    TODO: return something else? Called in add_model function.
    Returning 0,1 would cause an error on the condition the function is
        returned into.
    """
    # Return true indicates it has not been considered and so can be added
    al_name = alph(name)
    n_qub = get_num_qubits(name)
    if al_name in model_lists[n_qub]:
        return 'Previously Considered'  # todo -- make clear if in legacy or running db
    else:
        return 'New'


def num_parameters_from_name(name):
    t_str, p_str, max_t, max_p = DB.get_t_p_strings(name)
    # core_operator_dict = {
    #     'i' : np.eye(2),
    #     'x' : sigmax(),
    #     'y' : sigmay(),
    #     'z' : sigmaz()
    # }
    if(max_t >= max_p):
        # if more T's than P's in name, it has only one constituent.
        return 1
    else:
        # More P's indicates a sum at the highest dimension.
        return len(name.split(p_str))


def check_model_in_dict(name, model_dict):
    """
    Check whether the new model, name, exists in all previously considered models,
        held in model_lists.
    If name has not been previously considered, False is returned.
    """
    # Return true indicates it has not been considered and so can be added

    al_name = alph(name)
    n_qub = get_num_qubits(name)

    if al_name in model_dict[n_qub]:
        return True  # todo -- make clear if in legacy or running db
    else:
        return False


def check_model_exists(model_name, model_lists, db):
    # Return True if model exits; False if not.
    if consider_new_model(model_lists, model_name, db) == 'New':
        return False
    else:
        return True


def unique_model_pair_identifier(model_a_id, model_b_id):
    a = int(float(model_a_id))
    b = int(float(model_b_id))
    std = sorted([a, b])
    id_str = ''
    for i in range(len(std)):
        id_str += str(std[i])
        if i != len(std) - 1:
            id_str += ','

    return id_str


"""
Functions for accessing class instances of models within databse.
Useful to access information such as constituentes_operators.
Example usage:
$ ypz_model = get_qml_instance(db, 'yPz')
$ ypz_op = get_operator_instance(db, 'yPz')
$ operators = ypz_op.constituents_operators
"""


def get_qml_instance(db, name):
    location = get_location(db, name)
    return db.loc[location]["Model_Class_Instance"]


def get_operator_instance(db, name):
    location = get_location(db, name)
    return db.loc[location]["Operator_Instance"]


def remove_model(db, name):
    tmp_db = db[db['<Name>'] != name]
    return tmp_db


def move_to_legacy(db, legacy_db, name):
    legacy_db = legacy_db
    num_rows = len(legacy_db)
    model_instance = get_qml_instance(db, name)
    print("Model instance ", name, " moved to legacy db")
    new_row = pd.Series({
        '<Name>': name,
        'Param_Est_Final': model_instance.final_learned_params,
        'Epoch_Start': 0,  # TODO
        'Epoch_Finish': 10,  # TODO
        'ModelID': model_instance.model_id
    })

    legacy_db.loc[num_rows] = new_row


def model_branch_from_model_id(db, model_id):
    return db.loc[db['ModelID'] == model_id]['branchID'].item()


def model_id_from_name(db, name):
    name = alph(name)
    return db.loc[db['<Name>'] == name]['ModelID'].item()


def model_name_from_id(db, model_id):
    print("[DB] model_id:", model_id)
    return db.loc[db['ModelID'] == model_id]['<Name>'].item()


def index_from_name(db, name):
    name = alph(name)
    return db.loc[db['<Name>'] == name].index[0]


def index_from_model_id(db, model_id):
    return db.loc[db['ModelID'] == model_id].index[0]


def model_instance_from_id(db, model_id):
    idx = index_from_model_id(db, model_id)
    return db.loc[idx]["Model_Class_Instance"]


def reduced_model_instance_from_id(db, model_id):
    idx = index_from_model_id(db, model_id)
    return db.loc[idx]["Reduced_Model_Class_Instance"]


def list_model_id_in_branch(db, branchID):
    return list(db[db['branchID'] == branchID]['ModelID'])


def update_field(db, field, name=None, model_id=None,
                 new_value=None, increment=None):
    if name is not None:
        db.loc[db['<Name>'] == name, field] = new_value
    elif model_id is not None:
        db.loc[db['ModelID'] == model_id, field] = new_value


def pull_field(db, name, field):
    idx = get_location(db, name)
    if idx is not None:
        return db.loc[idx, field]
    else:
        print("Cannot update field -- model does not exist in database_framework.")


def model_names_on_branch(db, branchID):
    return list(db[(db['branchID'] == branchID)]['<Name>'])


def all_active_model_ids(db):
    return list(db[(db['Status'] == 'Active')]['ModelID'])


def active_model_ids_by_branch_id(db, branchID):
    return list(db[(db['branchID'] == branchID) & (
        db['Status'] == 'Active')]['ModelID'])


def all_unfinished_model_ids(db):
    return list(db[(db['Completed'] == False)]['ModelID'])


def all_unfinished_model_names(db):
    return list(db[(db['Completed'] == False)]['<Name>'])


def unfinished_model_ids_by_branch_id(db, branchID):
    return list(db[(db['branchID'] == branchID) &
                   (db['Completed'] == False)]['ModelID']
                )
