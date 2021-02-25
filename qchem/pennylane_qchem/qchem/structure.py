# Copyright 2018-2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This module contains the core functions for electronic structure calculations,
and converting the resulting data structures to forms understood by PennyLane."""
import os
import subprocess
from shutil import copyfile

import numpy as np
from openfermion import MolecularData, QubitOperator
from openfermion.transforms import bravyi_kitaev, get_fermion_operator, jordan_wigner
from openfermionpsi4 import run_psi4
from openfermionpyscf import run_pyscf

import pennylane as qml
from pennylane import Hamiltonian
from pennylane.wires import Wires


def _process_wires(wires, n_wires=None):
    r"""
    Checks and consolidates custom user wire mapping into a consistent, direction-free, `Wires`
    format. Used for converting between OpenFermion qubit numbering and Pennylane wire labels.

    Since OpenFermion's qubit numbering is always consecutive int, simple iterable types such as
    list, tuple, or Wires can be used to specify the 2-way `qubit index` <-> `wire label` mapping
    with indices representing qubits. Dict can also be used as a mapping, but does not provide any
    advantage over lists other than the ability to do partial mapping/permutation in the
    `qubit index` -> `wire label` direction.

    It is recommended to pass Wires/list/tuple `wires` since it's direction-free, i.e. the same
    `wires` argument can be used to convert both ways between OpenFermion and Pennylane. Only use
    dict for partial or unordered mapping.

    Args:
        wires (Wires, list, tuple, dict): User wire labels or mapping for Pennylane ansatz.
            For types Wires, list, or tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only int-keyed dict (for qubit-to-wire conversion) or
            consecutive-int-valued dict (for wire-to-qubit conversion) is accepted.
            If None, will be set to consecutive int based on ``n_wires``.
        n_wires (int): Number of wires used if known. If None, will be inferred from ``wires``; if
            ``wires`` is not available, will be set to 1.

    Returns:
        Wires: Cleaned wire mapping with indices corresponding to qubits and values
            corresponding to wire labels.

    **Example**

    >>> # consec int wires if no wires mapping provided, ie. identity map: 0<->0, 1<->1, 2<->2
    >>> _process_wires(None, 3)
    <Wires = [0, 1, 2]>

    >>> # List as mapping, qubit indices with wire label values: 0<->w0, 1<->w1, 2<->w2
    >>> _process_wires(['w0','w1','w2'])
    <Wires = ['w0', 'w1', 'w2']>

    >>> # Wires as mapping, qubit indices with wire label values: 0<->w0, 1<->w1, 2<->w2
    >>> _process_wires(Wires(['w0', 'w1', 'w2']))
    <Wires = ['w0', 'w1', 'w2']>

    >>> # Dict as partial mapping, int qubits keys to wire label values: 0->w0, 1 unchanged, 2->w2
    >>> _process_wires({0:'w0',2:'w2'})
    <Wires = ['w0', 1, 'w2']>

    >>> # Dict as mapping, wires label keys to consec int qubit values: w2->2, w0->0, w1->1
    >>> _process_wires({'w2':2, 'w0':0, 'w1':1})
    <Wires = ['w0', 'w1', 'w2']>
    """

    # infer from wires, or assume 1 if wires is not of accepted types.
    if n_wires is None:
        n_wires = len(wires) if isinstance(wires, (Wires, list, tuple, dict)) else 1

    # defaults to no mapping.
    if wires is None:
        return Wires(range(n_wires))

    if isinstance(wires, (Wires, list, tuple)):
        # does not care about the tail if more wires are provided than n_wires.
        wires = Wires(wires[:n_wires])

    elif isinstance(wires, dict):

        if all([isinstance(w, int) for w in wires.keys()]):
            # Assuming keys are taken from consecutive int wires. Allows for partial mapping.
            n_wires = max(wires) + 1
            labels = list(range(n_wires))  # used for completing potential partial mapping.
            for k, v in wires.items():
                if k < n_wires:
                    labels[k] = v
            wires = Wires(labels)
        elif set(range(n_wires)).issubset(set(wires.values())):
            # Assuming values are consecutive int wires (up to n_wires, ignores the rest).
            # Does NOT allow for partial mapping.
            wires = {v: k for k, v in wires.items()}  # flip for easy indexing
            wires = Wires([wires[i] for i in range(n_wires)])
        else:
            raise ValueError("Expected only int-keyed or consecutive int-valued dict for `wires`")

    else:
        raise ValueError(
            "Expected type Wires, list, tuple, or dict for `wires`, got {}".format(type(wires))
        )

    if len(wires) != n_wires:
        # check length consistency when all checking and cleaning are done.
        raise ValueError(
            "Length of `wires` ({}) does not match `n_wires` ({})".format(len(wires), n_wires)
        )

    return wires


def _exec_exists(prog):
    r"""Checks whether the executable program ``prog`` exists in any of the directories
    set in the ``PATH`` environment variable.

    Args:
        prog (str): name of the executable program

    Returns:
        boolean: ``True`` if the executable ``prog`` is found; ``False`` otherwise
    """
    for dir_in_path in os.environ["PATH"].split(os.pathsep):
        path_to_prog = os.path.join(dir_in_path, prog)
        if os.path.exists(path_to_prog):
            try:
                subprocess.call([path_to_prog], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError:
                return False
            return True
    return False


def read_structure(filepath, outpath="."):
    r"""Reads the structure of the polyatomic system from a file and returns
    a list with the symbols of the atoms in the molecule and a 1D array
    with the atomic positions in Cartesian coordinates.

    The `xyz <https://en.wikipedia.org/wiki/XYZ_file_format>`_ format is supported out of the box.
    If `Open Babel <https://openbabel.org/>`_ is installed,
    `any format recognized by Open Babel <https://openbabel.org/wiki/Category:Formats>`_
    is also supported. Additionally, the new file ``structure.xyz``,
    containing the input geometry, is created in a directory with path given by
    ``outpath``.

    Open Babel can be installed using ``apt`` if on Ubuntu:

    .. code-block:: bash

        sudo apt install openbabel

    or using Anaconda:

    .. code-block:: bash

        conda install -c conda-forge openbabel

    See the Open Babel documentation for more details on installation.

    Args:
        filepath (str): name of the molecular structure file in the working directory
            or the absolute path to the file if it is located in a different folder
        outpath (str): path to the output directory

    Returns:
        tuple[list, array]: symbols of the atoms in the molecule and their positions in Cartesian
        coordinates.

    **Example**

    >>> symbols, coordinates = read_structure('h2.xyz')
    >>> print(symbols, coordinates)
    ['H', 'H'] [ 0.    0.   -0.35  0.    0.    0.35]
    """

    obabel_error_message = (
        "Open Babel converter not found:\n"
        "If using Ubuntu or Debian, try: 'sudo apt install openbabel' \n"
        "If using openSUSE, try: 'sudo zypper install openbabel' \n"
        "If using CentOS or Fedora, try: 'sudo snap install openbabel' "
        "Open Babel can also be downloaded from http://openbabel.org/wiki/Main_Page, \n"
        "make sure you add it to the PATH environment variable. \n"
        "If Anaconda is installed, try: 'conda install -c conda-forge openbabel'"
    )

    extension = filepath.split(".")[-1].strip().lower()

    file_in = filepath.strip()
    file_out = os.path.join(outpath, "structure.xyz")

    if extension != "xyz":
        if not _exec_exists("obabel"):
            raise TypeError(obabel_error_message)
        try:
            subprocess.run(
                ["obabel", "-i" + extension, file_in, "-oxyz", "-O", file_out], check=True
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Open Babel error. See the following Open Babel "
                "output for details:\n\n {}\n{}".format(e.stdout, e.stderr)
            ) from e
    else:
        copyfile(file_in, file_out)

    symbols = []
    coordinates = []
    with open(file_out) as f:
        for line in f.readlines()[2:]:
            symbol, x, y, z = line.split()
            symbols.append(symbol)
            coordinates.append(float(x))
            coordinates.append(float(y))
            coordinates.append(float(z))

    return symbols, np.array(coordinates)


def meanfield(
    symbols,
    coordinates,
    name="molecule",
    charge=0,
    mult=1,
    basis="sto-3g",
    package="pyscf",
    outpath=".",
):  # pylint: disable=too-many-arguments
    r"""Generates a file from which the mean field electronic structure
    of the molecule can be retrieved.

    This function uses OpenFermion-PySCF and OpenFermion-Psi4 plugins to
    perform the Hartree-Fock (HF) calculation for the polyatomic system using the quantum
    chemistry packages ``PySCF`` and ``Psi4``, respectively. The mean field electronic
    structure is saved in an hdf5-formatted file in the directory
    ``os.path.join(outpath, package, basis)``.

    The charge of the molecule can be given to simulate cationic/anionic systems.
    Also, the spin multiplicity can be input to determine the number of unpaired electrons
    occupying the HF orbitals as illustrated in the figure below.

    |

    .. figure:: ../../_static/qchem/hf_references.png
        :align: center
        :width: 50%

    |

    Args:
        symbols (list[str]): symbols of the atomic species in the molecule
        coordinates (array[float]): 1D array with the atomic positions in Cartesian
            coordinates. The coordinates must be given in Angstroms and the size of the array
            should be ``3*N`` where ``N`` is the number of atoms.
        name (str): molecule label
        charge (int): net charge of the system
        mult (int): Spin multiplicity :math:`\mathrm{mult}=N_\mathrm{unpaired} + 1` for
            :math:`N_\mathrm{unpaired}` unpaired electrons occupying the HF orbitals.
            Possible values for ``mult`` are :math:`1, 2, 3, \ldots`. If not specified,
            a closed-shell HF state is assumed.
        basis (str): Atomic basis set used to represent the HF orbitals. Basis set
            availability per element can be found
            `here <www.psicode.org/psi4manual/master/basissets_byelement.html#apdx-basiselement>`_
        package (str): Quantum chemistry package used to solve the Hartree-Fock equations.
            Either ``'pyscf'`` or ``'psi4'`` can be used.
        outpath (str): path to output directory

    Returns:
        str: absolute path to the file containing the mean field electronic structure

    **Example**

    >>> symbols, coordinates = (['H', 'H'], np.array([ 0., 0., -0.35, 0., 0., 0.35]))
    >>> meanfield(symbols, coordinates, name="h2")
    ./pyscf/sto-3g/h2
    """

    if coordinates.size != 3 * len(symbols):
        raise ValueError(
            "The size of the array 'coordinates' has to be 3*len(symbols) = {};"
            " got 'coordinates.size' = {}".format(3 * len(symbols), coordinates.size)
        )

    package = package.strip().lower()

    if package not in ("psi4", "pyscf"):
        error_message = (
            "Integration with quantum chemistry package '{}' is not available. \n Please set"
            " 'package' to 'pyscf' or 'psi4'.".format(package)
        )
        raise TypeError(error_message)

    package_dir = os.path.join(outpath.strip(), package)
    basis_dir = os.path.join(package_dir, basis.strip())

    if not os.path.isdir(package_dir):
        os.mkdir(package_dir)
        os.mkdir(basis_dir)
    elif not os.path.isdir(basis_dir):
        os.mkdir(basis_dir)

    path_to_file = os.path.join(basis_dir, name.strip())

    geometry = [[symbol, tuple(coordinates[3 * i : 3 * i + 3])] for i, symbol in enumerate(symbols)]

    molecule = MolecularData(geometry, basis, mult, charge, filename=path_to_file)

    if package == "psi4":
        run_psi4(molecule, run_scf=1, verbose=0, tolerate_error=1)

    if package == "pyscf":
        run_pyscf(molecule, run_scf=1, verbose=0)

    return path_to_file


def active_space(electrons, orbitals, mult=1, active_electrons=None, active_orbitals=None):
    r"""Builds the active space for a given number of active electrons and active orbitals.

    Post-Hartree-Fock (HF) electron correlation methods expand the many-body wave function
    as a linear combination of Slater determinants, commonly referred to as configurations.
    This configurations are generated by exciting electrons from the occupied to the
    unoccupied HF orbitals as sketched in the figure below. Since the number of configurations
    increases combinatorially with the number of electrons and orbitals this expansion can be
    truncated by defining an active space.

    The active space is created by classifying the HF orbitals as core, active and
    external orbitals:

    - Core orbitals are always occupied by two electrons
    - Active orbitals can be occupied by zero, one, or two electrons
    - The external orbitals are never occupied

    |

    .. figure:: ../../_static/qchem/sketch_active_space.png
        :align: center
        :width: 50%

    |

    .. note::
        The number of active *spin*-orbitals ``2*active_orbitals`` determines the number of
        qubits required to perform the quantum simulations of the electronic structure
        of the many-electron system.

    Args:
        electrons (int): total number of electrons
        orbitals (int): total number of orbitals
        mult (int): Spin multiplicity :math:`\mathrm{mult}=N_\mathrm{unpaired} + 1` for
            :math:`N_\mathrm{unpaired}` unpaired electrons occupying the HF orbitals.
            Possible values for ``mult`` are :math:`1, 2, 3, \ldots`. If not specified,
            a closed-shell HF state is assumed.
        active_electrons (int): Number of active electrons. If not specified, all electrons
            are treated as active.
        active_orbitals (int): Number of active orbitals. If not specified, all orbitals
            are treated as active.

    Returns:
        tuple: lists of indices for core and active orbitals

    **Example**

    >>> electrons = 4
    >>> orbitals = 4
    >>> core, active = active_space(electrons, orbitals, active_electrons=2, active_orbitals=2)
    >>> print(core) # core orbitals
    [0]
    >>> print(active) # active orbitals
    [1, 2]
    """
    # pylint: disable=too-many-branches

    if active_electrons is None:
        ncore_orbs = 0
        core = []
    else:
        if active_electrons <= 0:
            raise ValueError(
                "The number of active electrons ({}) "
                "has to be greater than 0.".format(active_electrons)
            )

        if active_electrons > electrons:
            raise ValueError(
                "The number of active electrons ({}) "
                "can not be greater than the total "
                "number of electrons ({}).".format(active_electrons, electrons)
            )

        if active_electrons < mult - 1:
            raise ValueError(
                "For a reference state with multiplicity {}, "
                "the number of active electrons ({}) should be "
                "greater than or equal to {}.".format(mult, active_electrons, mult - 1)
            )

        if mult % 2 == 1:
            if active_electrons % 2 != 0:
                raise ValueError(
                    "For a reference state with multiplicity {}, "
                    "the number of active electrons ({}) should be even.".format(
                        mult, active_electrons
                    )
                )
        else:
            if active_electrons % 2 != 1:
                raise ValueError(
                    "For a reference state with multiplicity {}, "
                    "the number of active electrons ({}) should be odd.".format(
                        mult, active_electrons
                    )
                )

        ncore_orbs = (electrons - active_electrons) // 2
        core = list(range(ncore_orbs))

    if active_orbitals is None:
        active = list(range(ncore_orbs, orbitals))
    else:
        if active_orbitals <= 0:
            raise ValueError(
                "The number of active orbitals ({}) "
                "has to be greater than 0.".format(active_orbitals)
            )

        if ncore_orbs + active_orbitals > orbitals:
            raise ValueError(
                "The number of core ({}) + active orbitals ({}) can not be "
                "greater than the total number of orbitals ({})".format(
                    ncore_orbs, active_orbitals, orbitals
                )
            )

        homo = (electrons + mult - 1) / 2
        if ncore_orbs + active_orbitals <= homo:
            raise ValueError(
                "For n_active_orbitals={}, there are no virtual orbitals "
                "in the active space.".format(active_orbitals)
            )

        active = list(range(ncore_orbs, ncore_orbs + active_orbitals))

    return core, active


def decompose(hf_file, mapping="jordan_wigner", core=None, active=None):
    r"""Decomposes the molecular Hamiltonian into a linear combination of Pauli operators using
    OpenFermion tools.

    This function uses OpenFermion functions to build the second-quantized electronic Hamiltonian
    of the molecule and map it to the Pauli basis using the Jordan-Wigner or Bravyi-Kitaev
    transformation.

    Args:
        hf_file (str): absolute path to the hdf5-formatted file with the
            Hartree-Fock electronic structure
        mapping (str): Specifies the transformation to map the fermionic Hamiltonian to the
            Pauli basis. Input values can be ``'jordan_wigner'`` or ``'bravyi_kitaev'``.
        core (list): indices of core orbitals, i.e., the orbitals that are
            not correlated in the many-body wave function
        active (list): indices of active orbitals, i.e., the orbitals used to
            build the correlated many-body wave function

    Returns:
        QubitOperator: an instance of OpenFermion's ``QubitOperator``

    **Example**

    >>> decompose('./pyscf/sto-3g/h2', mapping='bravyi_kitaev')
    (-0.04207897696293986+0j) [] + (0.04475014401986122+0j) [X0 Z1 X2] +
    (0.04475014401986122+0j) [X0 Z1 X2 Z3] +(0.04475014401986122+0j) [Y0 Z1 Y2] +
    (0.04475014401986122+0j) [Y0 Z1 Y2 Z3] +(0.17771287459806262+0j) [Z0] +
    (0.17771287459806265+0j) [Z0 Z1] +(0.1676831945625423+0j) [Z0 Z1 Z2] +
    (0.1676831945625423+0j) [Z0 Z1 Z2 Z3] +(0.12293305054268105+0j) [Z0 Z2] +
    (0.12293305054268105+0j) [Z0 Z2 Z3] +(0.1705973832722409+0j) [Z1] +
    (-0.2427428049645989+0j) [Z1 Z2 Z3] +(0.1762764080276107+0j) [Z1 Z3] +
    (-0.2427428049645989+0j) [Z2]
    """

    # loading HF data from the hdf5 file
    molecule = MolecularData(filename=hf_file.strip())

    # getting the terms entering the second-quantized Hamiltonian
    terms_molecular_hamiltonian = molecule.get_molecular_hamiltonian(
        occupied_indices=core, active_indices=active
    )

    # generating the fermionic Hamiltonian
    fermionic_hamiltonian = get_fermion_operator(terms_molecular_hamiltonian)

    mapping = mapping.strip().lower()

    if mapping not in ("jordan_wigner", "bravyi_kitaev"):
        raise TypeError(
            "The '{}' transformation is not available. \n "
            "Please set 'mapping' to 'jordan_wigner' or 'bravyi_kitaev'.".format(mapping)
        )

    # fermionic-to-qubit transformation of the Hamiltonian
    if mapping == "bravyi_kitaev":
        return bravyi_kitaev(fermionic_hamiltonian)

    return jordan_wigner(fermionic_hamiltonian)


def _qubit_operator_to_terms(qubit_operator, wires=None):
    r"""Converts OpenFermion ``QubitOperator`` to a 2-tuple of coefficients and
    PennyLane Pauli observables.

    Args:
        qubit_operator (QubitOperator): Fermionic-to-qubit transformed operator in terms of
            Pauli matrices
        wires (Wires, list, tuple, dict): Custom wire mapping used to convert the qubit operator
            to an observable terms measurable in a PennyLane ansatz.
            For types Wires/list/tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only int-keyed dict (for qubit-to-wire conversion) is accepted.
            If None, will use identity map (e.g. 0->0, 1->1, ...).

    Returns:
        tuple[array[float], Iterable[pennylane.operation.Observable]]: coefficients and their
        corresponding PennyLane observables in the Pauli basis

    **Example**

    >>> q_op = 0.1*QubitOperator('X0') + 0.2*QubitOperator('Y0 Z2')
    >>> q_op
    0.1 [X0] +
    0.2 [Y0 Z2]
    >>> _qubit_operator_to_terms(q_op, wires=['w0','w1','w2','extra_wire'])
    (array([0.1, 0.2]), [Tensor(PauliX(wires=['w0'])), Tensor(PauliY(wires=['w0']), PauliZ(wires=['w2']))])
    """
    n_wires = (
        1 + max([max([i for i, _ in t]) if t else 1 for t in qubit_operator.terms])
        if qubit_operator.terms
        else 1
    )
    wires = _process_wires(wires, n_wires=n_wires)

    if not qubit_operator.terms:  # added since can't unpack empty zip to (coeffs, ops) below
        return np.array([0.0]), [qml.operation.Tensor(qml.Identity(wires[0]))]

    xyz2pauli = {"X": qml.PauliX, "Y": qml.PauliY, "Z": qml.PauliZ}

    coeffs, ops = zip(
        *[
            (
                coef,
                qml.operation.Tensor(*[xyz2pauli[q[1]](wires=wires[q[0]]) for q in term])
                if term
                else qml.operation.Tensor(qml.Identity(wires[0]))
                # example term: ((0,'X'), (2,'Z'), (3,'Y'))
            )
            for term, coef in qubit_operator.terms.items()
        ]
    )

    return np.real(np.array(coeffs)), list(ops)


def _terms_to_qubit_operator(coeffs, ops, wires=None):
    r"""Converts a 2-tuple of complex coefficients and PennyLane operations to
    OpenFermion ``QubitOperator``.

    This function is the inverse of ``_qubit_operator_to_terms``.

    Args:
        coeffs (array[complex]):
            coefficients for each observable, same length as ops
        ops (Iterable[pennylane.operation.Observable]): List of PennyLane observables as
            Tensor products of Pauli observables
        wires (Wires, list, tuple, dict): Custom wire mapping used to convert to qubit operator
            from an observable terms measurable in a PennyLane ansatz.
            For types Wires/list/tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only consecutive-int-valued dict (for wire-to-qubit conversion) is
            accepted. If None, will map sorted wires from all `ops` to consecutive int.

    Returns:
        QubitOperator: an instance of OpenFermion's ``QubitOperator``.

    **Example**

    >>> coeffs = np.array([0.1, 0.2])
    >>> ops = [
    ...     qml.operation.Tensor(qml.PauliX(wires=['w0'])),
    ...     qml.operation.Tensor(qml.PauliY(wires=['w0']), qml.PauliZ(wires=['w2']))
    ... ]
    >>> _terms_to_qubit_operator(coeffs, ops, wires=Wires(['w0', 'w1', 'w2']))
    0.1 [X0] +
    0.2 [Y0 Z2]
    """
    all_wires = Wires.all_wires([op.wires for op in ops], sort=True)

    if wires is not None:
        qubit_indexed_wires = _process_wires(wires,)
        if not set(all_wires).issubset(set(qubit_indexed_wires)):
            raise ValueError("Supplied `wires` does not cover all wires defined in `ops`.")
    else:
        qubit_indexed_wires = all_wires

    q_op = QubitOperator()
    for coeff, op in zip(coeffs, ops):

        extra_obsvbs = set(op.name) - {"PauliX", "PauliY", "PauliZ", "Identity"}
        if extra_obsvbs != set():
            raise ValueError(
                "Expected only PennyLane observables PauliX/Y/Z or Identity, "
                + "but also got {}.".format(extra_obsvbs)
            )

        # Pauli axis names, note s[-1] expects only 'Pauli{X,Y,Z}'
        pauli_names = [s[-1] if s != "Identity" else s for s in op.name]

        all_identity = all(obs.name == "Identity" for obs in op.obs)
        if (op.name == ["Identity"] and len(op.wires) == 1) or all_identity:
            term_str = ""
        else:
            term_str = " ".join(
                [
                    "{}{}".format(pauli, qubit_indexed_wires.index(wire))
                    for pauli, wire in zip(pauli_names, op.wires)
                    if pauli != "Identity"
                ]
            )

        # This is how one makes QubitOperator in OpenFermion
        q_op += coeff * QubitOperator(term_str)

    return q_op


def _qubit_operators_equivalent(openfermion_qubit_operator, pennylane_qubit_operator, wires=None):
    r"""Checks equivalence between OpenFermion :class:`~.QubitOperator` and Pennylane  VQE
    ``Hamiltonian`` (Tensor product of Pauli matrices).

    Equality is based on OpenFermion :class:`~.QubitOperator`'s equality.

    Args:
        openfermion_qubit_operator (QubitOperator): OpenFermion qubit operator represented as
            a Pauli summation
        pennylane_qubit_operator (pennylane.Hamiltonian): PennyLane
            Hamiltonian object
        wires (Wires, list, tuple, dict): Custom wire mapping used to convert to qubit operator
            from an observable terms measurable in a PennyLane ansatz.
            For types Wires/list/tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only int-keyed dict (for qubit-to-wire conversion) is accepted.
            If None, will map sorted wires from all Pennylane terms to consecutive int.

    Returns:
        (bool): True if equivalent
    """
    coeffs, ops = pennylane_qubit_operator.terms
    return openfermion_qubit_operator == _terms_to_qubit_operator(coeffs, ops, wires=wires)


def convert_observable(qubit_observable, wires=None):
    r"""Converts an OpenFermion :class:`~.QubitOperator` operator to a Pennylane VQE observable

    Args:
        qubit_observable (QubitOperator): Observable represented as an OpenFermion ``QubitOperator``
        wires (Wires, list, tuple, dict): Custom wire mapping used to convert the qubit operator
            to an observable terms measurable in a PennyLane ansatz.
            For types Wires/list/tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only int-keyed dict (for qubit-to-wire conversion) is accepted.
            If None, will use identity map (e.g. 0->0, 1->1, ...).

    Returns:
        (pennylane.Hamiltonian): Pennylane VQE observable. PennyLane :class:`~.Hamiltonian`
        represents any operator expressed as linear combinations of observables, e.g.,
        :math:`\sum_{k=0}^{N-1} c_k O_k`.

    **Example**

    >>> h_of = decompose('./pyscf/sto-3g/h2')
    >>> h_pl = convert_observable(h_of)
    >>> print(h_pl.coeffs)
    [-0.04207898  0.17771287  0.17771287 -0.24274281 -0.24274281  0.17059738
      0.04475014 -0.04475014 -0.04475014  0.04475014  0.12293305  0.16768319
      0.16768319  0.12293305  0.17627641]
    """

    return Hamiltonian(*_qubit_operator_to_terms(qubit_observable, wires=wires))


def molecular_hamiltonian(
    symbols,
    coordinates,
    name="molecule",
    charge=0,
    mult=1,
    basis="sto-3g",
    package="pyscf",
    active_electrons=None,
    active_orbitals=None,
    mapping="jordan_wigner",
    outpath=".",
    wires=None,
):  # pylint:disable=too-many-arguments
    r"""Generates the qubit Hamiltonian of a molecule.

    This function drives the construction of the second-quantized electronic Hamiltonian
    of a molecule and its transformation to the basis of Pauli matrices.

    #. OpenFermion-PySCF or OpenFermion-Psi4 plugins are used to launch
       the Hartree-Fock (HF) calculation for the polyatomic system using the quantum
       chemistry package ``PySCF`` or ``Psi4``, respectively.

       - The net charge of the molecule can be given to simulate
         cationic/anionic systems. Also, the spin multiplicity can be input
         to determine the number of unpaired electrons occupying the HF orbitals
         as illustrated in the left panel of the figure below.

       - The basis of Gaussian-type *atomic* orbitals used to represent the *molecular* orbitals
         can be specified to go beyond the minimum basis approximation. Basis set availability
         per element can be found
         `here <www.psicode.org/psi4manual/master/basissets_byelement.html#apdx-basiselement>`_

    #. An active space can be defined for a given number of *active electrons*
       occupying a reduced set of *active orbitals* in the vicinity of the frontier
       orbitals as sketched in the right panel of the figure below.

    #. Finally, the second-quantized Hamiltonian is mapped to the Pauli basis and
       converted to a PennyLane observable.

    |

    .. figure:: ../../_static/qchem/fig_mult_active_space.png
        :align: center
        :width: 90%

    |

    Args:
        symbols (list[str]): symbols of the atomic species in the molecule
        coordinates (array[float]): 1D array with the atomic positions in Cartesian
            coordinates. The coordinates must be given in Angstroms and the size of the array
            should be ``3*N`` where ``N`` is the number of atoms.
        name (str): name of the molecule
        charge (int): Net charge of the molecule. If not specified a a neutral system is assumed.
        mult (int): Spin multiplicity :math:`\mathrm{mult}=N_\mathrm{unpaired} + 1`
            for :math:`N_\mathrm{unpaired}` unpaired electrons occupying the HF orbitals.
            Possible values of ``mult`` are :math:`1, 2, 3, \ldots`. If not specified,
            a closed-shell HF state is assumed.
        basis (str): Atomic basis set used to represent the molecular orbitals. Basis set
            availability per element can be found
            `here <www.psicode.org/psi4manual/master/basissets_byelement.html#apdx-basiselement>`_
        package (str): quantum chemistry package (pyscf or psi4) used to solve the
            mean field electronic structure problem
        active_electrons (int): Number of active electrons. If not specified, all electrons
            are considered to be active.
        active_orbitals (int): Number of active orbitals. If not specified, all orbitals
            are considered to be active.
        mapping (str): transformation (``'jordan_wigner'`` or ``'bravyi_kitaev'``) used to
            map the fermionic Hamiltonian to the qubit Hamiltonian
        outpath (str): path to the directory containing output files
        wires (Wires, list, tuple, dict): Custom wire mapping for connecting to Pennylane ansatz.
            For types Wires/list/tuple, each item in the iterable represents a wire label
            corresponding to the qubit number equal to its index.
            For type dict, only int-keyed dict (for qubit-to-wire conversion) is accepted for
            partial mapping. If None, will use identity map.

    Returns:
        tuple[pennylane.Hamiltonian, int]: the fermionic-to-qubit transformed Hamiltonian
        and the number of qubits

    **Example**

    >>> symbols, coordinates = (['H', 'H'], np.array([ 0., 0., -0.35, 0., 0., 0.35]))
    >>> H, qubits = molecular_hamiltonian(symbols, coordinates)
    >>> print(qubits)
    4
    >>> print(H)
    (-0.04207897647782188) [I0]
    + (0.17771287465139934) [Z0]
    + (0.1777128746513993) [Z1]
    + (-0.24274280513140484) [Z2]
    + (-0.24274280513140484) [Z3]
    + (0.17059738328801055) [Z0 Z1]
    + (0.04475014401535161) [Y0 X1 X2 Y3]
    + (-0.04475014401535161) [Y0 Y1 X2 X3]
    + (-0.04475014401535161) [X0 X1 Y2 Y3]
    + (0.04475014401535161) [X0 Y1 Y2 X3]
    + (0.12293305056183801) [Z0 Z2]
    + (0.1676831945771896) [Z0 Z3]
    + (0.1676831945771896) [Z1 Z2]
    + (0.12293305056183801) [Z1 Z3]
    + (0.176276408043196) [Z2 Z3]
    """

    hf_file = meanfield(symbols, coordinates, name, charge, mult, basis, package, outpath)

    molecule = MolecularData(filename=hf_file)

    core, active = active_space(
        molecule.n_electrons, molecule.n_orbitals, mult, active_electrons, active_orbitals
    )

    h_of, qubits = (decompose(hf_file, mapping, core, active), 2 * len(active))

    return convert_observable(h_of, wires=wires), qubits


def excitations(electrons, orbitals, delta_sz=0):
    r"""Generates single and double excitations from a Hartree-Fock reference state.

    Single and double excitations can be generated by acting with the operators
    :math:`\hat T_1` and :math:`\hat T_2` on the Hartree-Fock reference state:

    .. math::

        && \hat{T}_1 = \sum_{r \in \mathrm{occ} \\ p \in \mathrm{unocc}}
        \hat{c}_p^\dagger \hat{c}_r \\
        && \hat{T}_2 = \sum_{r>s \in \mathrm{occ} \\ p>q \in
        \mathrm{unocc}} \hat{c}_p^\dagger \hat{c}_q^\dagger \hat{c}_r \hat{c}_s.


    In the equations above the indices :math:`r, s` and :math:`p, q` run over the
    occupied (occ) and unoccupied (unocc) *spin* orbitals and :math:`\hat c` and
    :math:`\hat c^\dagger` are the electron annihilation and creation operators,
    respectively.

    |

    .. figure:: ../../_static/qchem/sd_excitations.png
        :align: center
        :width: 80%

    |

    Args:
        electrons (int): Number of electrons. If an active space is defined, this
            is the number of active electrons.
        orbitals (int): Number of *spin* orbitals. If an active space is defined,
            this is the number of active spin-orbitals.
        delta_sz (int): Specifies the selection rules ``sz[p] - sz[r] = delta_sz`` and
            ``sz[p] + sz[p] - sz[r] - sz[s] = delta_sz`` for the spin-projection ``sz`` of
            the orbitals involved in the single and double excitations, respectively.
            ``delta_sz`` can take the values :math:`0`, :math:`\pm 1` and :math:`\pm 2`.

    Returns:
        tuple(list, list): lists with the indices of the spin orbitals involved in the
        single and double excitations

    **Example**

    >>> electrons = 2
    >>> orbitals = 4
    >>> singles, doubles = excitations(electrons, orbitals)
    >>> print(singles)
    [[0, 2], [1, 3]]
    >>> print(doubles)
    [[0, 1, 2, 3]]
    """

    if not electrons > 0:
        raise ValueError(
            "The number of active electrons has to be greater than 0 \n"
            "Got n_electrons = {}".format(electrons)
        )

    if orbitals <= electrons:
        raise ValueError(
            "The number of active spin-orbitals ({}) "
            "has to be greater than the number of active electrons ({}).".format(
                orbitals, electrons
            )
        )

    if delta_sz not in (0, 1, -1, 2, -2):
        raise ValueError(
            "Expected values for 'delta_sz' are 0, +/- 1 and +/- 2 but got ({}).".format(delta_sz)
        )

    # define the spin projection 'sz' of the single-particle states
    sz = np.array([0.5 if (i % 2 == 0) else -0.5 for i in range(orbitals)])

    singles = [
        [r, p]
        for r in range(electrons)
        for p in range(electrons, orbitals)
        if sz[p] - sz[r] == delta_sz
    ]

    doubles = [
        [s, r, q, p]
        for s in range(electrons - 1)
        for r in range(s + 1, electrons)
        for q in range(electrons, orbitals - 1)
        for p in range(q + 1, orbitals)
        if (sz[p] + sz[q] - sz[r] - sz[s]) == delta_sz
    ]

    return singles, doubles


def hf_state(electrons, orbitals):
    r"""Generates the occupation-number vector representing the Hartree-Fock state.

    The many-particle wave function in the Hartree-Fock (HF) approximation is a `Slater determinant
    <https://en.wikipedia.org/wiki/Slater_determinant>`_. In Fock space, a Slater determinant
    for :math:`N` electrons is represented by the occupation-number vector:

    .. math::

        \vert {\bf n} \rangle = \vert n_1, n_2, \dots, n_\mathrm{orbs} \rangle,
        n_i = \left\lbrace \begin{array}{ll} 1 & i \leq N \\ 0 & i > N \end{array} \right.,

    where :math:`n_i` indicates the occupation of the :math:`i`-th orbital.

    Args:
        electrons (int): Number of electrons. If an active space is defined, this
            is the number of active electrons.
        orbitals (int): Number of *spin* orbitals. If an active space is defined,
            this is the number of active spin-orbitals.

    Returns:
        array: NumPy array containing the vector :math:`\vert {\bf n} \rangle`

    **Example**

    >>> state = hf_state(2, 6)
    >>> print(state)
    [1 1 0 0 0 0]
    """

    if electrons <= 0:
        raise ValueError(
            "The number of active electrons has to be larger than zero; got 'electrons' = {}".format(
                electrons
            )
        )

    if electrons > orbitals:
        raise ValueError(
            "The number of active orbitals cannot be smaller than the number of active electrons;"
            " got 'orbitals'={} < 'electrons'={}".format(orbitals, electrons)
        )

    state = np.where(np.arange(orbitals) < electrons, 1, 0)

    return np.array(state)


def excitations_to_wires(singles, doubles, wires=None):
    r"""Map the indices representing the single and double excitations
    generated with the function :func:`~.excitations` to the wires that
    the Unitary Coupled-Cluster (UCCSD) template will act on.

    Args:
        singles (list[list[int]]): list with the indices ``r``, ``p`` of the two qubits
            representing the single excitation
            :math:`\vert r, p \rangle = \hat{c}_p^\dagger \hat{c}_r \vert \mathrm{HF}\rangle`
        doubles (list[list[int]]): list with the indices ``s``, ``r``, ``q``, ``p`` of the four
            qubits representing the double excitation
            :math:`\vert s, r, q, p \rangle = \hat{c}_p^\dagger \hat{c}_q^\dagger
            \hat{c}_r \hat{c}_s \vert \mathrm{HF}\rangle`
        wires (Iterable[Any]): Wires of the quantum device. If None, will use consecutive wires.

    The indices :math:`r, s` and :math:`p, q` in these lists correspond, respectively, to the
    occupied and virtual orbitals involved in the generated single and double excitations.

    Returns:
        tuple[list[list[Any]], list[list[list[Any]]]]: lists with the sequence of wires,
        resulting from the single and double excitations, that the Unitary Coupled-Cluster
        (UCCSD) template will act on.

    **Example**

    >>> singles = [[0, 2], [1, 3]]
    >>> doubles = [[0, 1, 2, 3]]
    >>> singles_wires, doubles_wires = excitations_to_wires(singles, doubles)
    >>> print(single_wires)
    [[0, 1, 2], [1, 2, 3]]
    >>> print(doubles_wires)
    [[[0, 1], [2, 3]]]

    >>> wires=['a0', 'b1', 'c2', 'd3']
    >>> singles_wires, doubles_wires = excitations_to_wires(singles, doubles, wires=wires)
    >>> print(singles_wires)
    [['a0', 'b1', 'c2'], ['b1', 'c2', 'd3']]
    >>> print(doubles_wires)
    [[['a0', 'b1'], ['c2', 'd3']]]
    """

    if (not singles) and (not doubles):
        raise ValueError(
            "'singles' and 'doubles' lists can not be both empty;\
            got singles = {}, doubles = {}".format(
                singles, doubles
            )
        )

    expected_shape = (2,)
    for single_ in singles:
        if np.array(single_).shape != expected_shape:
            raise ValueError(
                "Expected entries of 'singles' to be of shape (2,); got {}".format(
                    np.array(single_).shape
                )
            )

    expected_shape = (4,)
    for double_ in doubles:
        if np.array(double_).shape != expected_shape:
            raise ValueError(
                "Expected entries of 'doubles' to be of shape (4,); got {}".format(
                    np.array(double_).shape
                )
            )

    max_idx = 0
    if singles:
        max_idx = np.max(singles)
    if doubles:
        max_idx = max(np.max(doubles), max_idx)

    if wires is None:
        wires = range(max_idx + 1)
    elif len(wires) != max_idx + 1:
        raise ValueError("Expected number of wires is {}; got {}".format(max_idx + 1, len(wires)))

    singles_wires = []
    for r, p in singles:
        s_wires = [wires[i] for i in range(r, p + 1)]
        singles_wires.append(s_wires)

    doubles_wires = []
    for s, r, q, p in doubles:
        d1_wires = [wires[i] for i in range(s, r + 1)]
        d2_wires = [wires[i] for i in range(q, p + 1)]
        doubles_wires.append([d1_wires, d2_wires])

    return singles_wires, doubles_wires


def derivative(H, x, i, delta=0.00529):
    r"""Uses a finite difference approximation to compute the ``i``-th derivative
    :math:`\frac{\partial \hat{H}(x)}{\partial x_i}` of the electronic Hamiltonian
    evaluated at the nuclear coordinates ``x``.

    .. math::

        \frac{\partial \hat{H}(x)}{\partial x_i} \approx \frac{\hat{H}(x_i+\delta/2)
        - \hat{H}(x_i-\delta/2)}{\delta}

    Args:
        H (callable): function with signature ``H(x)`` that builds the electronic
            Hamiltonian of the molecule for a given set of nuclear coordinates ``x``
        x (array[float]): 1D array with the nuclear coordinates given in Angstroms.
            The size of the array should be ``3*N`` where ``N`` is the number of atoms
            in the molecule.
        i (int): index of the nuclear coordinate involved in the derivative
            :math:`\partial \hat{H}(x)/\partial x_i`
        delta (float): Step size in Angstroms used to displace the nuclear coordinate in the finite difference approximation.
            Its default value corresponds to 0.01 Bohr radii.

    Returns:
        pennylane.Hamiltonian: the derivative of the Hamiltonian
        :math:`\frac{\partial \hat{H}(x)}{\partial x_i}`

    **Example**

    >>> def H(x):
    ...     return qml.qchem.molecular_hamiltonian(['H', 'H'], x)[0]

    >>> x = np.array([0., 0., 0.35, 0., 0., -0.35])
    >>> print(derivative(H, x, 2))
    (-0.7763135665896025) [I0]
    + (-0.08534360836029752) [Z0]
    + (-0.08534360836029196) [Z1]
    + (0.26693410814904256) [Z2]
    + (0.2669341081490398) [Z3]
    + (-0.025233628787494015) [Z0 Z1]
    + (0.007216244400096865) [Y0 X1 X2 Y3]
    + (-0.007216244400096865) [Y0 Y1 X2 X3]
    + (-0.007216244400096865) [X0 X1 Y2 Y3]
    + (0.007216244400096865) [X0 Y1 Y2 X3]
    + (-0.03065428777395116) [Z0 Z2]
    + (-0.023438043373856375) [Z0 Z3]
    + (-0.023438043373856375) [Z1 Z2]
    + (-0.03065428777395116) [Z1 Z3]
    + (-0.024944077860444742) [Z2 Z3]
    """

    if not callable(H):
        error_message = (
            "{} object is not callable. \n"
            "'H' should be a callable function to build the electronic Hamiltonian 'H(x)'".format(
                type(H)
            )
        )
        raise TypeError(error_message)

    to_bohr = 1.8897261254535

    x_plus = x.copy()
    x_plus[i] += delta * 0.5

    x_minus = x.copy()
    x_minus[i] -= delta * 0.5

    return (H(x_plus) - H(x_minus)) * (delta * to_bohr) ** -1


def gradient(H, x, delta=0.00529):
    r"""Uses a finite difference approximation to compute the gradient
    :math:`\nabla_x \hat{H}(x)` of the electronic Hamiltonian :math:`\hat{H}(x)`
    for a given set of nuclear coordinates :math:`x`.

    Args:
        H (callable): function with signature ``H(x)`` that builds the electronic
            Hamiltonian for a given set of coordinates ``x``
        x (array[float]): 1D array with the coordinates in Angstroms. The size of the array
            should be ``3*N`` where ``N`` is the number of atoms in the molecule.
        delta (float): Step size in Angstroms used to displace the nuclear coordinates.
            Its default value corresponds to 0.01 Bohr radii.

    Returns:
        Iterable[pennylane.Hamiltonian]: list with the gradient vector :math:`\nabla_x \hat{H}(x)`.
        Each entry of the gradient is an operator.

    **Example**

    >>> def H(x):
    ...     return qml.qchem.molecular_hamiltonian(['H', 'H'], x)[0]

    >>> x = np.array([0., 0., 0.35, 0., 0., -0.35])
    >>> grad = gradient(H, x)
    >>> print(len(grad), grad[5])
    6 (0.7763135665895081) [I0]
    + (0.08534360836030584) [Z0]
    + (0.08534360836030307) [Z1]
    + (-0.26693410814900365) [Z2]
    + (-0.26693410814900087) [Z3]
    + (0.025233628787494015) [Z0 Z1]
    + (-0.007216244400096171) [Y0 X1 X2 Y3]
    + (0.007216244400096171) [Y0 Y1 X2 X3]
    + (0.007216244400096171) [X0 X1 Y2 Y3]
    + (-0.007216244400096171) [X0 Y1 Y2 X3]
    + (0.03065428777395116) [Z0 Z2]
    + (0.02343804337385915) [Z0 Z3]
    + (0.02343804337385915) [Z1 Z2]
    + (0.03065428777395116) [Z1 Z3]
    + (0.024944077860433636) [Z2 Z3]
    """

    grad = [derivative(H, x, i, delta=delta) for i in range(len(x))]

    return grad


def second_derivative(H, x, i, j, delta=0.00529):
    r"""Uses a finite difference approximation to compute the second-order derivative
    :math:`\frac{\partial^2 \hat{H}(x)}{\partial x_i \partial x_j}` of the electronic
    Hamiltonian evaluated at the nuclear coordinates :math:`x`.

    For :math:`x_i = x_j` the derivative is approximated as

    .. math::

        \frac{\partial^2 \hat{H}(x)}{\partial x_i^2} \approx
        \frac{\hat{H}(x_i + \delta) - 2 \hat{H}(x) + \hat{H}(x_i - \delta)}{\delta^2},

    and for :math:`x_i \neq x_j` is given by

    .. math::

        \frac{\partial^2 \hat{H}(x)}{\partial x_i \partial x_j} \approx
        \frac{\hat{H}(x_i + \delta/2, x_j + \delta/2) - \hat{H}(x_i - \delta/2, x_j + \delta/2)
        - \hat{H}(x_i + \delta/2, x_j - \delta/2) + \hat{H}(x_i - \delta/2, x_j - \delta/2)}
        {\delta^2}.

    Args:
        H (callable): function with signature ``H(x)`` that builds the electronic
            Hamiltonian of the molecule for a given set of nuclear coordinates ``x``
        x (array[float]): 1D array with the coordinates in Angstroms. The size of the
            array should be ``3*N`` where ``N`` is the number of atoms in the molecule.
        i (int): index of the :math:`i`-th coordinate
        j (int): index of the :math:`j`-th coordinate
        delta (float): Step size in Angstroms used to displace the nuclear coordinates.
            Its default value corresponds to 0.01 Bohr radii.

    Returns:
        pennylane.Hamiltonian: the second-order derivative
        :math:`\frac{\partial^2 \hat{H}(x)}{\partial x_i \partial x_j}` of the Hamiltonian

    **Example**

    >>> def H(x):
    ...     return qml.qchem.molecular_hamiltonian(['H', 'H'], x)[0]

    >>> x = np.array([0., 0., 0.35, 0., 0., -0.35])
    >>> print(second_derivative(H, x, i=0, j=3))
    (0.5868312587039344) [I0]
    + (0.06451526002110404) [Z0]
    + (0.06451526002054855) [Z1]
    + (-0.20178489650810605) [Z2]
    + (-0.20178489650810605) [Z3]
    + (0.019075588799274536) [Z0 Z1]
    + (-0.0054552078951808965) [Y0 X1 X2 Y3]
    + (0.0054552078951808965) [Y0 Y1 X2 X3]
    + (0.0054552078951808965) [X0 X1 Y2 Y3]
    + (-0.0054552078951808965) [X0 Y1 Y2 X3]
    + (0.02317330168492995) [Z0 Z2]
    + (0.017718093789749055) [Z0 Z3]
    + (0.017718093789749055) [Z1 Z2]
    + (0.02317330168492995) [Z1 Z3]
    + (0.018856528057322967) [Z2 Z3]
    """

    if not callable(H):
        error_message = (
            "{} object is not callable. \n"
            "'H' should be a callable function to build the electronic Hamiltonian 'H(x)'".format(
                type(H)
            )
        )
        raise TypeError(error_message)

    to_bohr = 1.8897261254535

    if i == j:
        x_p = x.copy()
        x_p[i] += delta

        x_m = x.copy()
        x_m[i] -= delta

        return (H(x_p) - 2.0 * H(x) + H(x_m)) * (delta * to_bohr) ** -2

    if i != j:
        x_pp = x.copy()
        x_pp[i] += delta * 0.5
        x_pp[j] += delta * 0.5

        x_mp = x.copy()
        x_mp[i] -= delta * 0.5
        x_mp[j] += delta * 0.5

        x_pm = x.copy()
        x_pm[i] += delta * 0.5
        x_pm[j] -= delta * 0.5

        x_mm = x.copy()
        x_mm[i] -= delta * 0.5
        x_mm[j] -= delta * 0.5

        return (H(x_pp) - H(x_mp) - H(x_pm) + H(x_mm)) * (delta * to_bohr) ** -2


def second_derivative_energy(H, x, i, j, ansatz, params, dev, hessian, delta=0.00529):
    r"""Computes the second-order derivative
    :math:`\frac{\partial^2 E(\theta^*(x), x)}{\partial x_i \partial x_j}` of the total energy
    evaluated at the nuclear coordinates :math:`x`.

    The expression to evaluate the second derivative of the energy is given by
    (`arXiv:1905.04054 <https://arxiv.org/abs/1905.04054>`_):

    .. math::

        \frac{\partial^2 E(\theta^*(x))}{\partial x_i \partial x_j} = \sum_a
        \frac{\partial \theta^*_a(x)}{\partial x_i} \frac{\partial}{\partial \theta_a}
        \frac{\partial E({\theta}^*(x), x)}{\partial x_j} + \langle \Psi(\theta^*(x)) \vert
        \frac{\partial^2 \hat{H}(x)}{\partial x_i \partial x_j} \vert \Psi(\theta^*(x)) \rangle.

    In the equation above, the energy derivatives with respect to the nuclear coordinates :math:`x`
    are obtained from the expectation value of Hamiltonian derivative

    .. math::

        \frac{\partial E({\theta}^*(x), x)}{\partial x_j} = \langle \Psi(\theta^*(x))
        \vert \frac{\partial \hat{H}(x)}{\partial x_j} \vert \Psi(\theta^*(x)) \rangle,

    and the derivative of the optimized circuit parameters :math:`\theta^*(x)` with respect
    to :math:`x` are obtained by solving the system of linear equations,

    .. math::

        \sum_b \frac{\partial^2 E(\theta^*(x), x)}{\partial \theta_a \partial \theta_b}
        \frac{\partial \theta^*_b(x)}{\partial x_i} = -\frac{\partial}{\partial \theta_a}
        \frac{\partial E(\theta^*(x), x)}{\partial x_i}.

    Args:
        H (callable): function with signature ``H(x)`` that builds the electronic
            Hamiltonian of the molecule for a given set of nuclear coordinates ``x``
        x (array[float]): 1D array with the coordinates in Angstroms. The size of the
            array should be ``3*N`` where ``N`` is the number of atoms in the molecule.
        i (int): index of the :math:`i`-th coordinate
        j (int): index of the :math:`j`-th coordinate
        ansatz (callable): the ansatz for the circuit before the final measurement step
        params (array[float]): optimized circuit parameters :math:`\theta^*(x)`
        dev (Device): device where the calculations of the expectation values should be executed
        hessian (array[float, float]): matrix of dimension ``params.size`` containing the Hessian
            of the energy with respect to the circuit parameters
            :math:`\frac{\partial^2 E(\theta^*(x), x)}{\partial \theta_a \partial \theta_b}
        delta (float): Step size in Angstroms used to displace the nuclear coordinates 
            Its default value corresponds to 0.01 Bohr radii.

    Returns:
        float: the derivative
        :math:`\frac{\partial^2 E(\theta^*(x), x)}{\partial x_i \partial x_j}` of the total
        energy

    **Example**
    
    """

    if not callable(H):
        error_message = (
            "{} object is not callable. \n"
            "'H' should be a callable function to build the electronic Hamiltonian 'H(x)'".format(
                type(H)
            )
        )
        raise TypeError(error_message)

    # \nabla_\theta dE/dxj
    dE_j = qml.ExpvalCost(ansatz, derivative(x, H, j, delta=delta), dev)
    grad_dE_j = np.array(qml.grad(dE_j)(params))

    # \nabla_\theta dE/dxi
    dE_i = qml.ExpvalCost(ansatz, derivative(x, H, i, delta=delta), dev)
    grad_dE_i = np.array(qml.grad(dE_i)(params))

    # solve linear system of equations to find 'dtheta(x)/dxi'
    dtheta_i = np.linalg.solve(hessian, -grad_dE_i)

    # compute '<d2H/dxidxj>'
    expval_d2H_ij = qml.ExpvalCost(ansatz, second_derivative(x, H, i, j, delta=delta), dev)(params)

    return np.dot(dtheta_i, grad_dE_j) + expval_d2H_ij


__all__ = [
    "read_structure",
    "meanfield",
    "active_space",
    "decompose",
    "convert_observable",
    "molecular_hamiltonian",
    "hf_state",
    "excitations",
    "excitations_to_wires",
    "derivative",
    "gradient",
    "second_derivative",
    "_qubit_operator_to_terms",
    "_terms_to_qubit_operator",
    "_qubit_operators_equivalent",
]
