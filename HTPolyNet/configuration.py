# -*- coding: utf-8 -*-
"""

read cfg file
@author: huang, abrams

"""
import json
import yaml
import os
import logging
import numpy as np
from copy import deepcopy
from itertools import product

from HTPolyNet.molecule import Molecule, Reaction

def _determine_sequence(m,moldict):
    if not moldict[m].generator:
        return [m]
    thisseq=[]
    for rid,mname in moldict[m].generator.reactants.items():
        thisseq.extend(_determine_sequence(mname,moldict))
    return thisseq

class Configuration:
    def __init__(self):
        self.cfgFile = ''
        self.Title = ''
        ''' List of (Molecule, count) '''
        self.initial_composition = []
        ''' Dictionary of name:Molecule '''
        self.molecules = {}
        ''' List of molecules for which symmetry-equivalence is requested '''
        self.use_sea=[]
        ''' List of Reaction instances '''
        self.reactions = []
        ''' all other parameters in cfg file '''
        self.parameters = {}

    @classmethod
    def read(cls,filename):
        extension=filename.split('.')[-1]
        if extension=='json':
            return cls.read_json(filename)
        elif extension=='yaml' or extension=='yml':
            return cls.read_yaml(filename)
        else:
            raise Exception(f'Unknown config file extension {extension}')

    @classmethod
    def read_json(cls,filename):
        inst=cls()
        inst.cfgFile=filename
        with open(filename,'r') as f:
            inst.basedict=json.load(f)
        inst.parse()
        return inst

    @classmethod
    def read_yaml(cls,filename):
        inst=cls()
        inst.cfgFile=filename
        with open(filename,'r') as f:
            inst.basedict=yaml.safe_load(f)
        inst.parse()
        return inst
    
    def parse(self):
        privileged_keys=['reactions','Title','use_sea','initial_composition']
        self.Title=self.basedict.get('Title','No Title Provided')
        ''' reactions must declare molecules '''
        self.reactions=[Reaction(r) for r in self.basedict['reactions']]
        for R in self.reactions:
            for rkey,r in R.reactants.items():
                ''' reactants do not get assigned generators if they are *only* reactants '''
                if not r in self.molecules:
                    self.molecules[r]=Molecule(r)
                else:
                    logging.debug(f'Reactant {r} in reaction {R.name} is already on the global Molecules list')
                    if self.molecules[r].generator:
                        logging.debug(f'{r} is a product of reaction {self.molecules[r].generator.name}')
            if not R.product in self.molecules:
                self.molecules[R.product]=Molecule(R.product,generator=R)
        self.initial_composition=self.basedict.get('initial_composition',[])
        ''' any molecules lists in the initial composition
        may not have been declared in reactions '''
        for item in self.initial_composition:
            m=item['molecule']
            if m not in self.molecules:
                self.molecules[m]=Molecule(m)

        for mname,M in self.molecules.items():
            M.sequence=_determine_sequence(mname,self.molecules)
            logging.debug(f'Sequence of {mname}: {list(enumerate(M.sequence))}')

        self.use_sea=self.basedict.get('use_sea',[])
        for m in self.use_sea:
            if not m in self.molecules:
                logging.error(f'Configuration {self.cfgFile} references undeclared molecule {m} in use_sea')
                logging.error(f'Molecules must be declared in reactions or initial_composition')
                raise Exception('Configuration error')

        self.privileged_items={}
        for k in privileged_keys:
            self.privileged_items[k]=self.basedict[k]
            del self.basedict[k]

        self.parameters=self.basedict
        if not 'cpu' in self.parameters:
            self.parameters['cpu']=os.cpu_count()
        return self

    def symmetry_expand_reactions(self,unique_molecules):
        extra_reactions=[]
        extra_molecules={}
        for R in self.reactions:
            sym_partners={}
            R.sym=0
            seas={}
            for atom in R.atoms.values():
                atomName=atom['atom']
                resNum=atom['resid']
                molecule=unique_molecules[R.reactants[atom['reactant']]]
                molName=molecule.name
                seq=molecule.sequence
                # generate symmetry-equivalent realizations based on sequence
                seas[molName]=[]
                for rname in seq:
                    residue=unique_molecules[rname]
                    assert len(residue.sequence)==1 # this is a monomer!!
                    clu=residue.atoms_w_same_attribute_as(find_dict={'atomName':atomName},    
                                                        same_attribute='sea-idx',
                                                        return_attribute='atomName')
                    sp=list(clu)
                    seas[molName].append(sp)
                # we can look at products in other reactions to see...
                # 
                # logging.debug('\n'+molecule.Coords.A.to_string())
                # logging.debug(f'Symmetry_expand: Reaction {R.name} atomName {atomName} resNum {resNum} resName {resName} molname {molName}')
                #product=self.molecules[R['product']]
                # Asea=molecule.Coords.get_atom_attribute('sea-idx',{'atomName':atomName,'resNum':resNum})
                # Aclu=molecule.Coords.get_atoms_w_attribute('atomName',{'sea-idx':Asea,'resNum':resNum})
                # Aclu=np.delete(Aclu,np.where(Aclu==atomName))
                # Aclu=molecule.atoms_w_same_attribute_as(find_dict={'atomName':atomName,'resNum':resNum},same_attribute='sea-idx',return_attribute='atomName')
                # # Aclu.remove(atomName)
                # sp=list(Aclu)
                # for aa in Aclu:
                #     sp.append(aa)
                # sym_partners[atomName]=sp
            if len(R.reactants)>1: # not intramolecular; make all combinations
                logging.debug(f'sending to product: {[x for x in seas.values()]}')
                P=product(*[x for x in seas.values()])
                O=next(P)
            else: # intramolecular; keep partners together
                logging.debug(f'sym_partners.values() {sym_partners.values()}')
                P=[p for p in zip(*[x for x in seas.values()])]
                O=P[0]
                P=P[1:]
            logging.debug(f'Original atoms for symmetry expansion: {O}')
            idx=1
            # TODO: properly build up symmetry-related reactions
            
            for p in P:
                logging.debug(f'Replicating {R.name} using {p}')
                newR=deepcopy(R)
                newR.sym=idx
                newR.name+=f'-{idx}'
                newR.product+=f'-{idx}'
                for rxnum,rxname in newR.reactants.items():
                    if rxname+f'-{idx}' in extra_molecules:
                        newR.reactants[rxnum]=rxname+f'-{idx}'
                idx+=1
                for a,o in zip(p,O):
                    for atom,oatom in zip(newR.atoms,R.atoms):
                        if R.atoms[oatom]['atom']==o:
                            newR.atoms[atom]['atom']=a
                extra_reactions.append(newR)
                newP=Molecule(name=newR.product,generator=newR)
                original_molecule=unique_molecules[R.name]
                # newP.generate(available_molecules=self.molecules,**self.parameters)
                extra_molecules[newR.product]=newP

        for nR in extra_reactions:
            logging.debug(f'symmetry-derived reaction {nR.name} atoms {nR.atoms}')
        self.reactions.extend(extra_reactions)
        self.molecules.update(extra_molecules)
        return extra_molecules

    def get_reaction(self,product_name):
        if '-' in product_name:
            base_product_name,sym=product_name.split('-')
        else:
            base_product_name,sym=product_name,'0'
        for r in self.reactions:
            if r.product==base_product_name:
                return r
        return None

    def calculate_maximum_conversion(self):
        N={}
        for item in self.initial_composition:
            N[item['molecule']]=item['count']
        Bonds=[]
        Atoms=[]
        logging.debug(f'CMC: extracting atoms from {len(self.reactions)} reactions')
        for R in self.reactions:
            for b in R.bonds:
                A,B=b['atoms']
                a,b=R.atoms[A],R.atoms[B]
                aan,ban=a['atom'],b['atom']
                ari,bri=a['resid'],b['resid']
                arn,brn=R.reactants[a['reactant']],R.reactants[b['reactant']]
                if arn!=brn:
                    az,bz=a['z'],b['z']
                    ia=(aan,ari,arn,az)
                    ib=(ban,bri,brn,bz)
                    b=(ia,ib)
                    if ia not in Atoms and arn in N:
                        Atoms.append(ia)
                    if ib not in Atoms and brn in N:
                        Atoms.append(ib)
                    if b not in Bonds and arn in N and brn in N:
                        Bonds.append(b)
        # logging.debug(f'atomset: {Atoms}')
        Z=[]
        for a in Atoms:
            Z.append(a[3]*N[a[2]])
        # logging.debug(f'Z: {Z}')
        # logging.debug(f'bondset: {Bonds}')
        MaxB=[]
        for B in Bonds:
            a,b=B
            az=Z[Atoms.index(a)]
            bz=Z[Atoms.index(b)]
            MaxB.append(min(az,bz))
            Z[Atoms.index(a)]-=MaxB[-1]
            Z[Atoms.index(b)]-=MaxB[-1]
        # logging.debug(f'MaxB: {MaxB} {sum(MaxB)}')
        return sum(MaxB)
