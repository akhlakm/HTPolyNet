#!/usr/bin/env python
"""
@author: huang, abrams
"""
import logging
import os
import shutil
import argparse as ap
#import numpy as np
from copy import deepcopy
from itertools import product

''' intrapackage imports '''
from HTPolyNet.configuration import Configuration
from HTPolyNet.coordinates import Coordinates, BTRC
# import HTPolyNet.searchBonds as searchBonds
# import HTPolyNet.genBonds as genBonds
# import HTPolyNet.generateChargeDb as generateChargeDb
# import HTPolyNet.generateTypeInfo as generateTypeInfo

#from HTPolyNet.ambertools import GAFFParameterize
from HTPolyNet.topology import Topology

#from HTPolyNet.software import Software, Command
#from HTPolyNet.countTime import *
#from HTPolyNet.projectfilesystem import ProjectFileSystem, Library
import HTPolyNet.projectfilesystem as pfs
import HTPolyNet.software as software
#from HTPolyNet.command import Command
from HTPolyNet.gromacs import insert_molecules, grompp_and_mdrun, density_trace
#from HTPolyNet.molecule import Molecule

class HTPolyNet:
    ''' Class for a single HTPolyNet runtime session '''
    def __init__(self,cfgfile='',restart=False):
        logging.info(f'Generating new htpolynet runtime!')
        logging.info(software.to_string())
        if cfgfile=='':
            logging.error('HTPolyNet requires a configuration file.\n')
            raise RuntimeError('HTPolyNet requires a configuration file.')
        self.cfg=Configuration.read(cfgfile)
        logging.info(f'Configuration: {cfgfile}')
        ''' initialize an empty topology and coordinates '''
        self.Topology=Topology(system=self.cfg.Title)
        self.Coordinates=Coordinates()
        self.cfg.parameters['restart']=restart
        if self.cfg.parameters['restart']:
            logging.info(f'***** THIS IS A RESTART *****')

    def checkout(self,filename,altpath=None):
        if not pfs.checkout(filename):
            searchpath=pfs.local_data_searchpath()
            logging.info(f'No {filename} found in libraries; checking local data searchpath {searchpath}')
            if altpath:
                searchpath.append(altpath)
                logging.info(f'and alternative path {altpath}')
            for p in searchpath:
                fullfilename=os.path.join(p,filename)
                if os.path.exists(fullfilename):
                    basefilename=os.path.basename(filename)
                    shutil.copyfile(fullfilename,basefilename)
                    logging.info(f'Found at {fullfilename}')
                    return True
            return False
        return True

    def generate_molecules(self,force_parameterization=False,force_sea_calculation=False,force_checkin=False):
        logging.info('*'*10+' GENERATING MOLECULE PATTERNS '+'*'*10)
        self.molecules={}
        for mname,M in self.cfg.molecules.items():
            M.set_origin('unparameterized')
        cwd=pfs.cd('molecules')
        checkin=pfs.checkin
        exists=pfs.exists
        ''' Each molecule implied by the cfg is 'generated' here, either by
            reading from the library or direct parameterization.  In some cases,
            the molecule is to be generated by a reaction; if so, it's 
            `generator` attribute will be a Reaction instance '''
        msg=', '.join([m for m in self.cfg.molecules])
        logging.debug(f'Will generate {msg}')
        all_made=all([(m in self.molecules and M.get_origin()!='unparameterized') for m,M in self.cfg.molecules.items()])
        iter=1
        while not all_made:
            logging.debug(f'Pass number {iter} through molecules in cfg')
            iter+=1
            ''' We have to generate molecules that act as precursors to other molecules before
                we create the molecules that need those precursors '''
            for mname,M in self.cfg.molecules.items():
                ''' a molecule with no generator specified is a monomer and can be generated by
                    parameterizing an existing mol2 file or reading in a previous parameterization
                    from the library '''
                if mname not in self.molecules:
                    logging.debug(f'Molecule {mname} not in runtime molecule list')
                    if force_parameterization or not M.previously_parameterized():
                        logging.debug(f'Parameterization of {mname} requested -- can we generate {mname}?')
                        generatable=(not M.generator) or (all([m in self.molecules for m in M.generator.reactants.values()]))
                        if generatable:
                            logging.info(f'Generating {mname}')
                            M.generate(available_molecules=self.molecules,**self.cfg.parameters)
                            for ex in ['mol2','top','itp','gro']:
                                checkin(f'molecules/parameterized/{mname}.{ex}',overwrite=force_checkin)
                            M.set_origin('newly parameterized')
                        else:
                            logging.debug(f'Cannot generate {mname} yet.  Bailing.')
                            continue
                    else:
                        logging.info(f'Fetching parameterized {mname}')
                        for ex in ['mol2','top','itp','gro']:
                            self.checkout(f'molecules/parameterized/{mname}.{ex}')
                        M.read_topology(f'{mname}.top')
                        M.read_coords(f'{mname}.gro')
                        M.set_origin('previously parameterized')
                    ''' The cfg allows user to indicate whether or not to determine and use
                        symmetry-equivalent atoms in any molecule. '''
                    if mname in self.cfg.use_sea:
                        if force_sea_calculation or not exists(f'molecules/parameterized/{mname}.sea'):
                            logging.info(f'Doing SEA calculation on {mname}')
                            M.calculate_sea()
                            M.Coords.write_atomset_attributes(['sea-idx'],f'{M.name}.sea')
                            checkin(f'molecules/parameterized/{mname}.sea',overwrite=force_checkin)
                        else:
                            logging.debug(f'Reading sea data into {M.name}')
                            self.checkout(f'molecules/parameterized/{mname}.sea')
                            M.read_atomset_attributes(f'{mname}.sea',attributes=['sea-idx'])
                    assert M.get_origin()!='unparameterized'
                    self.molecules[mname]=M
                    logging.info(f'Generated {mname}')
            all_made=all([(m in self.molecules and M.get_origin()!='unparameterized') for m,M in self.cfg.molecules.items()])
            logging.info(f'Done making molecules: {all_made}')
            for m in self.cfg.molecules:
                logging.info(f'Mol {m} made? {m in self.molecules} -- origin: {M.get_origin()}')
            
        ''' We need to copy all symmetry info down to atoms in each molecule based on the reactants
            used to generate them.  We then must use this symmetry information to expand the list
            of Reactions '''
        for M in self.molecules.values():
            M.inherit_sea_from_reactants(self.molecules,self.cfg.use_sea)
        self.cfg.symmetry_expand_reactions()
        precursors=[M for M in self.molecules.values() if not M.generator]
        for M in precursors:
            M.propagate_z(self.cfg.reactions,self.molecules)
        reaction_products=[M for M in self.molecules.values() if M.generator]
        for M in reaction_products:
            M.propagate_z(self.cfg.reactions,self.molecules)
        for M in self.molecules.values():
            logging.debug(f'Ring detector for {M.name}')
            M.label_ring_atoms(M.Topology.ring_detector())

    def initialize_global_topology(self,filename='init.top'):
        ''' Create a full gromacs topology that includes all directives necessary 
            for an initial liquid simulation.  This will NOT use any #include's;
            all types will be explicitly in-lined. '''
        cwd=pfs.cd('systems')
        if os.path.isfile('init.top'):
            logging.info(f'init.top already exists in {cwd} but we will rebuild it anyway!')
        ''' for each monomer named in the cfg, either parameterize it or fetch its parameterization '''
        for item in self.cfg.initial_composition:
            M=self.molecules[item['molecule']]
            N=item['count']
            t=deepcopy(M.Topology)
            t.adjust_charges(0)
            t.rep_ex(N)
            logging.info(f'initialize_topology merging {N} copies of {M.name} into global topology')
            self.Topology.merge(t)
        logging.info(f'Extended topology has {self.Topology.atomcount()} atoms.')
        cwd=pfs.cd('systems')
        self.Topology.to_file(filename)
        logging.info(f'Wrote {filename} to {cwd}')

    def setup_liquid_simulation(self):
        # go to the results path, make the directory 'init', cd into it
        cwd=pfs.next_results_dir(restart=self.cfg.parameters['restart'])
        # fetch unreacted init.top amd all monomer gro's 
        # from parameterization directory
        self.checkout('init.top',altpath=pfs.subpath('systems'))
        for n in self.cfg.molecules.keys():
            self.checkout(f'molecules/parameterized/{n}.gro',altpath=pfs.subpath('molecules'))
        # fetch mdp files from library, or die if not found
        self.checkout('mdp/em.mdp')
        self.checkout('mdp/npt-1.mdp')
        if 'initial_boxsize' in self.cfg.parameters:
            boxsize=self.cfg.parameters['initial_boxsize']
        elif 'initial_density' in self.cfg.parameters:
            mass_kg=self.Topology.total_mass(units='SI')
            V0_m3=mass_kg/self.cfg.parameters['initial_density']
            L0_m=V0_m3**(1./3.)
            L0_nm=L0_m*1.e9
            logging.info(f'Initial density {self.cfg.parameters["initial_density"]} kg/m^3 and total mass {mass_kg:.3f} kg dictate an initial box side length of {L0_nm:.3f} nm')
            boxsize=[L0_nm,L0_nm,L0_nm]
        # extend system, make gro file
        clist=self.cfg.initial_composition
        c_togromacs={}
        for cc in clist:
            c_togromacs[cc['molecule']]=cc['count']
        m_togromacs={}
        for mname,M in self.cfg.molecules.items():
            if mname in c_togromacs:
                m_togromacs[mname]=M
        for m,M in m_togromacs.items():
            logging.info(f'Molecule to gromacs: {m} ({M.name})')
        for m,c in c_togromacs.items():
            logging.info(f'Composition to gromacs: {m} {c}')
        if not os.path.exists('init.gro'): 
            msg=insert_molecules(m_togromacs,c_togromacs,boxsize,'init')
        else:
            logging.info(f'init.gro exists -- not inserting any more molecules')
        self.Coordinates=Coordinates.read_gro('init.gro')
        self.Coordinates.inherit_attributes_from_molecules(['z','cycle-idx'],self.cfg.molecules)
        # for r in self.Coordinates.rings():
        #     logging.debug(f'a ring: {r}')
        self.Coordinates.write_atomset_attributes(['cycle-idx','z'],'init.grx')
        self.Coordinates.make_ringlist()
        assert self.Topology.atomcount()==self.Coordinates.atomcount(), 'Error: Atom count mismatch'
        logging.info('Generated init.top and init.gro.')

    def do_liquid_simulation(self):
        if os.path.exists('npt-1.gro') and self.cfg.parameters['restart']:
            logging.info(f'npt-1.gro exists in {os.getcwd()}; skipping initial NPT md.')
        else:
            msg=grompp_and_mdrun(gro='init',top='init',out='min-1',mdp='em')
            # TODO: modify this to run in stages until volume is equilibrated
            msg=grompp_and_mdrun(gro='min-1',top='init',out='npt-1',mdp='npt-1')
            logging.info('Generated configuration npt-1.gro\n')
        density_trace('npt-1')
        
        sacmol=Coordinates.read_gro('npt-1.gro')
        # ONLY copy posX, posY, and poxZ attributes!
        self.Coordinates.copy_coords(sacmol)
        self.Coordinates.box=sacmol.box.copy()
        if os.path.exists('npt-1.glc'):
            logging.info('npt-1.glc exists; no need to populate linkcell')
            self.Coordinates.linkcell_initialize(self.cfg.parameters['SCUR_cutoff'],populate=False)
            self.Coordinates.read_atomset_attributes('npt-1.glc',attributes=['linkcell-idx'])
            self.Coordinates.linkcell.make_memberlists(self.Coordinates.A)
        else:
            self.Coordinates.linkcell_initialize(self.cfg.parameters['SCUR_cutoff'])
            self.Coordinates.write_atomset_attributes(['linkcell-idx'],'npt-1.glc')
        pfs.cd('root')

    def SCUR(self):
        # Search - Connect - Update - Relax
        logging.info('*'*10+' SEARCH - CONNECT - UPDATE - RELAX  BEGINS '+'*'*10)
        max_nxlinkbonds=int(self.Coordinates.A['z'].sum()/2) # only for a stoichiometric system
        logging.debug(f'SCUR: max_nxlinkbonds {max_nxlinkbonds}')
        if max_nxlinkbonds==0:
            logging.warning(f'Apparently there are no crosslink bonds to be made! (sum of z == 0)')
            return
        scur_complete=False
        scur_search_radius=self.cfg.parameters['SCUR_cutoff']
        desired_conversion=self.cfg.parameters['conversion']
        radial_increment=self.cfg.parameters.get('SCUR_radial_increment',0.5)
        maxiter=self.cfg.parameters.get('maxSCURiter',20)
        iter=0
        curr_nxlinkbonds=0
        while not scur_complete:
            logging.info(f'SCUR iteration {iter} begins')
            scur_complete=True
            # TODO: everything -- identify bonds less than radius
            # make bonds, relax
            num_newbonds=self.scur_make_bonds(scur_search_radius)
            # TODO: step-wise gromacs minimization and relaxation with "turn-on" of
            # bonded parameters
            curr_nxlinkbonds+=num_newbonds
            curr_conversion=curr_nxlinkbonds/max_nxlinkbonds
            scur_complete=curr_conversion>desired_conversion
            scur_complete=scur_complete or iter>=maxiter
            if not scur_complete:
                if num_newbonds==0:
                    logging.info(f'No new bonds in SCUR iteration {iter}')
                    logging.info(f'-> updating search radius to {scur_search_radius}')
                    scur_search_radius += radial_increment
                    # TODO: prevent search radius from getting too large for box
                    logging.info(f'-> updating search radius to {scur_search_radius}')
            iter+=1
            logging.info(f'SCUR iteration {iter} ends (maxiter {maxiter})')
            logging.info(f'Current conversion: {curr_conversion}')
            logging.info(f'   SCUR complete? {scur_complete}')
        logging.info(f'SCUR iterations complete.')
        # TODO: any post-cure reactions handled here

    def scur_make_bonds(self,radius):
        adf=self.Coordinates.A
        raset=adf[adf['z']>0]  # this view will be used for downselecting to potential A-B partners
        newbonds=[]
        ''' generate the list of new bonds to make '''
        for R in self.cfg.reactions:
            if R.stage=='post-cure':
                continue
            logging.debug(f'*** BONDS from reaction {R.name}')
            for bond in R.bonds:
                A=R.atoms[bond['atoms'][0]]
                B=R.atoms[bond['atoms'][1]]
                logging.debug(f'  -> bond {bond}: A {A}  B {B}')
                aname=A['atom']
                aresname=R.reactants[A['reactant']]
                aresid=A['resid']
                az=A['z']
                bname=B['atom']
                bresname=R.reactants[B['reactant']]
                bresid=B['resid']
                bz=B['z']
                Aset=raset[(raset['atomName']==aname)&(raset['resName']==aresname)&(raset['z']==az)]
                Bset=raset[(raset['atomName']==bname)&(raset['resName']==bresname)&(raset['z']==bz)]
                Pbonds=list(product(Aset['globalIdx'].to_list(),Bset['globalIdx'].to_list()))
                logging.debug(f'*** {len(Pbonds)} potential bonds')
                passbonds=[]
                bondtestoutcomes={k:0 for k in BTRC}
                # TODO: Parallelize
                for p in Pbonds:
                    RC=self.Coordinates.bondtest(p,radius)
                    bondtestoutcomes[RC]+=1
                    if RC==BTRC.passed:
                        passbonds.append((p,R.product))
                logging.debug(f'*** {len(passbonds)} out of {len(Pbonds)} bonds pass initial filter')
                logging.debug(f'Bond test outcomes:')
                for k,v in bondtestoutcomes.items():
                    logging.debug(f'   {str(k)}: {v}')
                newbonds.extend(passbonds)
                #logging.debug(f'     Aset {Aset.shape[0]}\n{Aset.to_string()}')
                #logging.debug(f'     Bset {Bset.shape[0]}\n{Bset.to_string()}')
                # logging.debug('Here are some potential pairs based on name/resname/z')
                # P=product(Aset.iterrows(),Bset.iterrows())
                # for p in P:
                #     logging.debug(p)
                '''
                TODO
                - FOR EACH BOND
                    - make selections of A-set and B-set reactive atoms for each bond in this reaction
                    - search for A-B pairs and make a list of potential bonds
                    - for each potential bond
                        - determine if it is allowed based on single-bond criteria
                           - within cutoff
                           - no ring piercing
                           - no loops or short circuits
                        - add it as a 2-tuple of global indices to the newbonds[] list, include template
                '''

        ''' TODO: let potential bonds compete with each other to see which ones move forward '''

        ''' make the new bonds '''
        if len(newbonds)>0:
            for p in newbonds:
                logging.debug(f'potential bond {p}')
            # TODO: all the hard stuff
            # - make this bond, update indexes of atoms in bonds yet to be made
            # - determine reaction template for this bond and find the product molecule
            # - map the system atoms to the product template bond atoms + neighbors to degree-x 
            #   and transfer charges from template
            # write gro/top
            # grompp_and_run minimization + NPT relaxation
            # update coords
            pass
        return 0

    def initreport(self):
        print(self.cfg)
        print()
        print(self.software)

    def main(self,**kwargs):
        force_parameterization=kwargs.get('force_parameterization',False)
        force_sea_calculation=kwargs.get('force_sea_calculation',False)
        force_checkin=kwargs.get('force_checkin',False)
        self.generate_molecules(
            force_parameterization=force_parameterization,force_sea_calculation=force_sea_calculation,force_checkin=force_checkin
        )
        self.initialize_global_topology()
        self.setup_liquid_simulation()
        self.do_liquid_simulation()
        self.SCUR()

def info():
    print('This is some information on your installed version of HTPolyNet')
    pfs.info()
    software.info()

def cli():
    parser=ap.ArgumentParser()
    parser.add_argument('command',type=str,default=None,help='command (init, info, run)')
    parser.add_argument('-cfg',type=str,default='',help='input config file')
    parser.add_argument('-lib',type=str,default='',help='local library, assumed flat')
    parser.add_argument('-log',type=str,default='htpolynet_runtime.log',help='log file')
    parser.add_argument('-restart',default=False,action='store_true',help='restart in latest proj dir')
    parser.add_argument('--force-parameterization',default=False,action='store_true',help='force GAFF parameterization of any input mol2 structures')
    parser.add_argument('--force-sea-calculation',default=False,action='store_true',help='force calculation of symmetry-equivalent atoms in any input mol2 structures')
    parser.add_argument('--force-checkin',default=False,action='store_true',help='force check-in of any generated parameter files to the system library')
    args=parser.parse_args()

    ''' set up logging '''
    logging.basicConfig(filename=args.log,encoding='utf-8',filemode='w',format='%(asctime)s %(message)s',level=logging.DEBUG)
    logging.info('HTPolyNet Runtime begins.')
    logging.info(f'Restart? {args.restart}')
    ''' set up the project file system and access to HTPolyNet libraries '''
    userlib=None
    if args.lib!='':
        userlib=args.lib
    pfs.pfs_setup(root=os.getcwd(),verbose=True,reProject=args.restart,userlibrary=userlib)
    software.sw_setup()

    if args.command=='info':
        info()
    elif args.command=='run':
        a=HTPolyNet(cfgfile=args.cfg,restart=args.restart)
        a.main(force_checkin=args.force_checkin,force_parameterization=args.force_parameterization,force_sea_calculation=args.force_sea_calculation)
    else:
        print(f'HTPolyNet command {args.command} not recognized')
    
    logging.info('HTPolynet Runtime ends.')
