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
  - constituent operators (names and matrices) [i.e. those which are summed to give model]
  - total matrix
  - number of qubits (dimension)

The database is generated by the function launch_db. E.g. usage: 

  $ db, model_db, model_lists = DataBase.launch_db(gen_list=gen_list)

This returns: 

  - db: "running database", info on dlog likelihood, etc.
  - model_db: info on construction of model, i.e. constituent operators etc.
  - model_lists = list of lists containing alphabetised model names. When a new model is considered, it should be compared against models of identical dimension (number of qubits) by alhpabetical name. If the alphabetical name is found in, e.g. model_lists[3], it has already been considered and the QML should be terminated.


To fill the data base, a list of generators are passed to launch_db. 
These are strings corresponding to unique models, e.g. 'xTy' means pauli_x TENSOR_PROD pauli_y 
(see Naming_Convention.pdf). 
These names are used to generate instances of the operator class (defined here). 
This class computes, based on the name, what the constituent operator names, matrices, total matrix, etc.
of the given model are, and fills these values into the model_db. 

e.g. usage of operator: 
  $ name='xPyTz'
  $ test_op = operator(name)
  $ print(test_op.name)
  $ print(test_op.matrix)
  $ print(test_op.constituent_operators

"""


import numpy as np
import itertools as itr

import os as os
import sys as sys 
import pandas as pd
import warnings
import hashlib


import Evo as evo
from QML import *

global paulis_list
paulis_list = {'i' : np.eye(2), 'x' : evo.sigmax(), 'y' : evo.sigmay(), 'z' : evo.sigmaz()}



"""
------ ------ Operator Class ------ ------
"""

class operator():
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

    Constituents of an operator are operators of the same dimension which sum to give the operator.
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
      -- uniquely identifies equivalent operators for comparison against previously considered models
    -
    """
    def __init__(self, name): 
        self.name = name
    
    @property
    def constituents_names(self):
        """
        List of constituent operators names.
        """
        t_str, p_str, max_t, max_p = get_t_p_strings(self.name)
        paulis_list = {'i' : np.eye(2), 'x' : evo.sigmax(), 'y' : evo.sigmay(), 'z' : evo.sigmaz()}
        if(max_t >= max_p):
            # if more T's than P's in name, it has only one constituent. 
            return [self.name]
        else: 
            # More P's indicates a sum at the highest dimension. 
            return self.name.split(p_str)

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
        mtx = empty_array_of_same_dim(self.name)
        for i in self.constituents_operators:
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
    
    
"""
Functions for use by operator class to parse string (name) and prodcue relevent operators, lists etc.
"""

def get_num_qubits(name):
    """
    Parse string and determine number of qubits this operator acts on. 
    """
    max_t_found = 0 
    t_str=''
    while name.count(t_str+'T')>0:
        t_str=t_str+'T'

    num_qubits = len(t_str) + 1
    return num_qubits
    

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
          ## In case of empty separator, split by anything into one string    
          p_str = 'RRR'
        
        sep_by_p = name.split(p_str)
        for element in sep_by_p:
            list_by_p_sep.append(get_acted_on_qubits(element))

        for i in range(len(list_by_p_sep)):
            to_add= list(set(list_by_p_sep[i]) - set(running_list))
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
          ## In case of empty separator, split by anything into one string    
          p_str = 'RRR'

        sep_by_p = name.split(p_str)
        for element in sep_by_p:
            list_by_sep.append(fill_qubits_acted_on_list, element)
    
    
    qubits_acted_on = []
    fill_qubits_acted_on_list(qubits_acted_on,name)
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
        i=max_t
        this_t_str = t_str
        broken_down = string_to_analyse.split(this_t_str)
        lhs = broken_down[0]
        rhs = broken_down[1]
        if rhs !='i':
            qubits_acted_on.append(i+1)

        if max_t == 1:
            if lhs!='i':
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
    while name.count(t_str+'T')>0:
        t_str=t_str+'T'

    while name.count(p_str+'P')>0:
        p_str=p_str+'P'

    max_t = len(t_str)
    max_p = len(p_str)

    return t_str, p_str, max_t, max_p        
    
def find_max_letter(string, letter):
    """
    Find largest instance of consecutive given 'letter'.
    Return largest instance and length of that instance. 
    """
    letter_str=''
    while string.count(letter_str+letter)>0:
        letter_str=letter_str+letter

    return len(letter_str), letter_str


def empty_array_of_same_dim(name):
    """
    Parse name to find size of system it acts on. 
    Produce an empty matrix of that dimension and return it. 
    """
    t_str=''
    while name.count(t_str+'T')>0:
        t_str=t_str+'T'

    num_qubits = len(t_str) +1
    dim = 2**num_qubits
    #print("String: ", name, " has NQubits: ", num_qubits)
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
    
    if p_max == 0 and t_max ==0 and p_max ==0 :
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
    if  p_max==m_max and p_max > t_max:
        string = p_str
        list_elements = name.split(p_str)
        
        for i in range(len(list_elements)):
            list_elements[i] = alph(list_elements[i])
        sorted_list = sorted(list_elements)
        linked_sorted_list = p_str.join(sorted_list)
        return linked_sorted_list
        
    if ltr=='P' and p_max==1:
        sorted_spread = sorted(spread)
        out = string.join(sorted_spread)
        return out
    elif ltr=='P' and p_max>1:
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

    if(max_p == 0 and max_t==0):
        pauli_symbol = inp
        return paulis_list[pauli_symbol] 

    elif(max_t==0):
        return compute(inp)
    else:
        to_tens = inp.split(t_str)
        #print("To tens: ", to_tens)
        running_tens_prod=compute(to_tens[0])
        #print("Split by ", t_str, " : \n", to_tens)
        for i in range(1,len(to_tens)):
            max_p, p_str = find_max_letter(to_tens[i], "P")
            max_t, t_str = find_max_letter(to_tens[i], "T")
            #print("To tens [i=", i, "]:\n", to_tens[i] )
            rhs = compute(to_tens[i])
            running_tens_prod = np.kron(running_tens_prod, rhs)
        #print("RESULT ", t_str, " : ", inp, ": \n", running_tens_prod)
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

    if(max_p == 0 and max_t==0):
        pauli_symbol = inp
        return paulis_list[pauli_symbol] 

    elif max_p==0:
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

    if(max_m == 0 and max_t==0 and max_p == 0 ):
        pauli_symbol = inp
        return paulis_list[pauli_symbol] 

    elif max_m ==0:
        return compute(inp)
    
    else:   
        to_mult = inp.split(m_str)
        #print("To mult : ", to_mult)
        t_str=''
        while inp.count(t_str+'T')>0:
            t_str=t_str+'T'

        num_qubits = len(t_str) +1
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

    max_p, p_str = find_max_letter(inp, "P")
    max_t, t_str = find_max_letter(inp, "T")
    max_m, m_str = find_max_letter(inp, "M")

    if(max_m == 0 and max_t==0 and max_p == 0):
        pauli_symbol = inp
        return paulis_list[pauli_symbol] 
    elif max_m > max_t:
        return compute_m(inp)
    elif max_t >= max_p:
        return compute_t(inp)
    else:
        return compute_p(inp)    




"""
------ ------ Database declaration and functions ------ ------
"""

"""
Initial distribution to sample from, normal_dist
"""
#TODO: change mean and var?
from qinfer import NormalDistribution
normal_dist=NormalDistribution(mean=0.5, var=0.05)  
normal_dist_width = 0.25

"""
QML parameters
#TODO: maybe these need to be changed
"""
n_particles = 2000
n_experiments = 300
#true_operator_list = [evo.sigmax(), evo.sigmay()]
true_operator_list = np.array([evo.sigmax(), evo.sigmay()])

#xtx = operator('xTx')
ytz = operator('yTz')

true_operator_list = np.array([ ytz.matrix] )


def launch_db(RootN_Qbit=[0], N_Qubits=1, gen_list=[], true_ops=[], true_params=[]):
    """
    Inputs:
    TODO
    RootN_Qbit: TODO
    N_Qubits: TODO
    gen_list: list of strings corresponding to model names. 
    
    Outputs: 
      - db: "running database", info on log likelihood, etc.
      - model_db: info on construction of model, i.e. constituent operators etc.
      - model_lists = list of lists containing alphabetised model names. When a new model is considered, it     

    Usage: 
        $ gen_list = ['xTy, yPz, iTxTTy] # Sample list of model names
        $ running_db, model_db, model_lists = DataBase.launch_db(gen_list=gen_list)
    
    """
    generators = []
    total_model_list = []
    qml_instances = []
#     TODO: Is this the absolute total ever???? Could define this as a global variable at top of file. 
    Max_N_Qubits = 13
    model_lists = {}
    for j in range(1, Max_N_Qubits):
        model_lists[j] = []
    
    for i in gen_list:
        generators.append(operator(i))
        qml_instances.append(ModelLearningClass(name=i))
        alph_model_name = alph(i)
        num_qubits = get_num_qubits(i)
        #model_lists[num_qubits].append(alph_model_name)

    #sim_ops = [ [] for _ in range(len(gen_list))]
    for i in range(len(qml_instances)):
        qml_inst = qml_instances[i] 
        op = generators[i]
        
        true_param_list = []
        for j in range(np.shape(true_operator_list)[1]):
            true_param_list.append(0.3)

        sim_ops=[]
        #sim_ops[i] = []
        for j in range(op.num_constituents):
            sim_ops.append(normal_dist.sample()[0,0])
#            sim_ops[i].append(normal_dist.sample())
        constituent_list = op.constituents_operators
        sim_ops = [sim_ops]
        qml_inst.InitialiseNewModel(

#          trueoplist = true_operator_list,
#          modeltrueparams = true_param_list,
          trueoplist = true_ops,
          modeltrueparams = true_params,
          simoplist = op.constituents_operators,
#          simparams = sim_ops[i],
          simparams = sim_ops,
          numparticles = n_particles,
          gaussian=False
        )

    legacy_db = pd.DataFrame({
        '<Name>' : [ ], 
        'Param_Est_Final' : [],
        'Epoch_Start' : [],
        'Epoch_Finish' : [],
    })
        
    # if N_qubits defined: work out generator list.
    # Or should number qubits be implied by gen list?


    db = pd.DataFrame({
        '<Name>' : [ ], 
        'Alph_Name' : [],
        'Status' : [], #TODO can get rid?
        'Selected' : [], #TODO what's this for?
        'TreeID' : [], # TODO proper tree id's,
        #'Param_Estimates' : sim_ops,
        #'Estimates_Dist_Width' : [normal_dist_width for gen in generators],
        'Model_Class_Instance' : [],
        'Operator_Instance' : [],
        'Epoch_Start' : [],
        })
        
    for model_name in gen_list: 
        add_model(model_name=model_name, running_database=db, model_lists=model_lists, true_ops=true_ops, true_params=true_params, epoch=0)  

    """
    db = pd.DataFrame({
        '<Name>' : [ gen.name for gen in generators], 
        'Status' : 'Ready', #TODO can get rid?
        'Selected' : False, #TODO what's this for?
        'TreeID' : [0 for gen in generators ], # TODO proper tree id's,
        #'Param_Estimates' : sim_ops,
        #'Estimates_Dist_Width' : [normal_dist_width for gen in generators],
        'Model_Class_Instance' : qml_instances,
        'Operator_Instance' : [gen for gen in generators],
        'Epoch_Start' : [0 for gen in generators],
        })  
    """    
    return db, legacy_db, model_lists


def add_model(model_name, running_database, model_lists, epoch=0, true_ops=[], true_params=[] ):
    """
    Function to add a model to the existing databases. 
    First checks whether the model already exists. 
    If so, does not add model to databases.
      TODO: do we want to return False in this case and use as a check in QMD?
    
    Inputs: 
      - model_name: new model name to be considered and added if new. 
      - running_database: Database (output of launch_db) containing info on log likelihood etc. 
      - model_lists: output of launch_db. A list of lists containing every previously considered model, categorised by dimension. 
      
    Outputs: 
      TODO: return True if added; False if previously considered? 
      
    Effect: 
      - If model hasn't been considered before, 
          Adds a row to running_database containing all columns of those.     
    """    
    
    alph_model_name = alph(model_name)
    model_num_qubits = get_num_qubits(model_name)
    
    
    if consider_new_model(model_lists, model_name, running_database)=='New':
        model_lists[model_num_qubits].append(alph_model_name)
    
        print("Model ", model_name, " not previously considered -- adding.")
        op = operator(model_name)
        num_rows = len(running_database)
        qml_instance = ModelLearningClass(name=model_name)
        true_param_list = []
        for j in range(len(true_operator_list)):
            true_param_list.append(0.3)

        sim_pars = []
        for j in range(op.num_constituents):
          sim_pars.append(normal_dist.sample()[0,0])
        # add model_db_new_row to model_db and running_database
        # Note: do NOT use pd.df.append() as this copies total DB,
        # appends and returns copy.
        qml_instance.InitialiseNewModel(
#          trueoplist = true_operator_list,
#          modeltrueparams = true_param_list,
          trueoplist = true_ops,
          modeltrueparams = true_params,
          simoplist = op.constituents_operators,
          simparams = [sim_pars],
          numparticles = n_particles,
          gaussian=False
        )
        
        # Add to running_database, same columns as initial gen_list
        
        running_db_new_row = pd.Series({
            '<Name>': op.name,
            'Alph_Name' : op.alph_name,
            'Status' : 'Ready', 
            'Selected' : False, 
            'TreeID' : 0, #TODO make argument of add_model fnc,
            'Param_Estimates' : sim_pars,
            'Estimates_Dist_Width' : normal_dist_width,
            'Model_Class_Instance' : qml_instance,
            'Operator_Instance' : op,
            'Epoch_Start' : 0, #TODO fill in
        })

        running_database.loc[num_rows] = running_db_new_row      
        
    else:
        location = consider_new_model(model_lists, model_name, running_database)
        print("Model", alph_model_name, " previously considered at location", location)  



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
    If name has been previously considered, the corresponding location in db is returned. 
    TODO: return something else? Called in add_model function. 
    """
    # Return true indicates it has not been considered and so can be added
    al_name = alph(name)
    n_qub = get_num_qubits(name)
    if al_name in model_lists[n_qub]:
        location = get_location_by_alph_name(db, al_name)
        return location
    else: 
        return 'New'


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
    tmp_db = db[db['<Name>']!=name]
    return tmp_db

def move_to_legacy(db, legacy_db, name):
    legacy_db = legacy_db
    num_rows = len(legacy_db)
    model_instance = get_qml_instance(db, name)
    print("Model instance : ", model_instance, " for model ", name)
    new_row = pd.Series({
        '<Name>' : name, 
        'Param_Est_Final' : model_instance.FinalParams,
        'Epoch_Start' : 0, #TODO
        'Epoch_Finish' : 10  #TODO
    })

    legacy_db.loc[num_rows] = new_row         
