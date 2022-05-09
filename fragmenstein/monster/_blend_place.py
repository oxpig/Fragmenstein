########################################################################################################################
__doc__ = \
    """
This is inherited by MonsterPlace
    """

########################################################################################################################

from rdkit.Chem import rdmolops

import itertools, operator
import json
from collections import Counter
from collections import defaultdict
from typing import Optional, Dict, List, Tuple, Set
from warnings import warn

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem import rdFMCS, rdMolAlign, rdmolops
from rdkit.Chem import rdmolops
from rdkit.Geometry.rdGeometry import Point3D

from ._communal import _MonsterCommunal
from ._merge import _MonsterMerge
from .unmerge_mapper import Unmerge


class _MonsterBlend(_MonsterMerge):
    # placement dependent methods



    # @classmethod #why was this a classmethod
    def full_blending(self) -> None:
        """
        a single scaffold is made (except for ``.unmatched``)
        """
        self.mol_options = [self.simply_merge_hits()]
        scaffold = self.posthoc_refine(self.mol_options[0])
        chimera = self.make_chimera(scaffold)

        # atom_map is filled via `self.get_mcs_mapping(target_mol, template_mol)`:
        self.positioned_mol = self.place_from_map(target_mol=self.initial_mol,
                                                  template_mol=chimera,
                                                  atom_map=None,
                                                  random_seed=self.random_seed)
        self.keep_copy(scaffold, 'scaffold')
        self.keep_copy(chimera, 'chimera')

    def partial_blending(self) -> None:
        """
        multiple possible scaffolds for placement and best is chosen
        """
        self.mol_options = self.partially_blend_hits()  # merger of hits
        unrefined_scaffold, mode_index = self.pick_best()
        used = unrefined_scaffold.GetProp('_Name').split('-')
        self.unmatched = [h.GetProp('_Name') for h in self.hits if h.GetProp('_Name') not in used]
        scaffold = self.posthoc_refine(unrefined_scaffold)
        chimera = self.make_chimera(scaffold, mode_index)

        # atom_map is filled via `self.get_mcs_mapping(target_mol, template_mol)`:
        self.positioned_mol = self.place_from_map(target_mol=self.positioned_mol,
                                                  template_mol=chimera,
                                                  atom_map=None,
                                                  random_seed=self.random_seed)
        self.keep_copy(scaffold, 'scaffold')
        self.keep_copy(chimera, 'chimera')

    def no_blending(self, broad=False) -> None:
        """
        no merging is done. The hits are mapped individually. Not great for small fragments.
        """
        maps = {}
        for template in self.hits:
            if broad:
                self.journal.debug('Merge ligands: False. Broad search: True')
                pair_atom_maps, _ = self.get_mcs_mappings(self.initial_mol, template)
                maps[template.GetProp('_Name')] = pair_atom_maps
            else:
                self.journal.debug('Merge ligands: False. Broad search: False')
                pair_atom_maps_t = self._get_atom_maps(self.initial_mol, template,
                                                       **self.strict_matching_mode)
                pair_atom_maps = [dict(p) for p in pair_atom_maps_t]
                maps[template.GetProp('_Name')] = pair_atom_maps
        um = Unmerge(followup=self.initial_mol,
                     mols=self.hits,
                     maps=maps,
                     no_discard=self.throw_on_discard)

        self.unmatched = [m.GetProp('_Name') for m in um.disregarded]
        if self.throw_on_discard and len(self.unmatched):
            raise ConnectionError(f'{self.unmatched} was rejected.')
        self.journal.debug(f'followup to scaffold {um.combined_map}')
        # ------------------ places the atoms with known mapping ------------------
        placed = self.place_from_map(target_mol=self.initial_mol,
                                     template_mol=um.combined_bonded,
                                     atom_map=um.combined_map,
                                     random_seed=self.random_seed)

        self.keep_copy(um.combined, 'scaffold')
        self.keep_copy(um.combined_bonded, 'chimera')

        alts = zip(um.combined_bonded_alternatives, um.combined_map_alternatives)
        placed_options = [self.place_from_map(target_mol=self.initial_mol,
                                             template_mol=mol,
                                             atom_map=mappa,
                                             random_seed=self.random_seed) for mol, mappa in alts]
        # ------------------ Averages the overlapping atoms ------------------
        if um.pick != -1:  # override present!
            self.journal.debug(f'override present: {um.pick}')
            self.positioned_mol = self.posthoc_refine(placed)
            self.mol_options = [self.posthoc_refine(mol) for mol in placed_options]
        else:
            mols: List[Chem.Mol] = [self.posthoc_refine(mol) for mol in [placed] + placed_options]
            self.positioned_mol: Chem.Mol = self.get_best_scoring(mols)
            mols.remove(self.positioned_mol)
            self.mol_options: List[Chem.Mol] = mols

    # ================= Blend hits ===================================================================================

    def partially_blend_hits(self, hits: Optional[List[Chem.Mol]] = None) -> List[Chem.Mol]:
        """
        This is the partial merge algorithm, wherein the hits are attempted to be combined.
        If the combination is bad. It will not be combined.
        Returning a list of possible options.
        These will have the atoms changed too.

        :param hits:
        :param distance:
        :return:
        """

        if hits is None:
            hits = sorted(self.hits, key=lambda h: h.GetNumAtoms(), reverse=True)
        for hi, hit in enumerate(hits):
            # fallback naming.
            if not hit.HasProp('_Name') or hit.GetProp('_Name').strip() == '':
                hit.SetProp('_Name', f'hit{hi}')

        ## a dodgy hit is a hit with inconsistent mapping between three.
        def get_dodgies(skippers):
            dodgy = []
            for hit0, hit1, hit2 in itertools.combinations(hits, 3):
                hn0 = hit0.GetProp('_Name')
                hn1 = hit1.GetProp('_Name')
                hn2 = hit2.GetProp('_Name')
                if any([hit in skippers for hit in (hn0, hn1, hn2)]):
                    continue
                for a, b in inter_mapping[(hn0, hn1)].items():
                    if a in inter_mapping[(hn0, hn2)] and b in inter_mapping[(hn1, hn2)]:
                        if inter_mapping[(hn0, hn2)][a] != inter_mapping[(hn1, hn2)][b]:
                            # TODO: THIS IS A BAD OPTION:
                            # if all([m.GetAtomWithIdx(i).IsInRing() for m, i in ((hit0, a),
                            #                                                     (hit1, b),
                            #                                                     (hit2, inter_mapping[(hn0, hn2)][a]),
                            #                                                     (hit2, inter_mapping[(hn1, hn2)][b]))]):
                            #     pass
                            # else:
                            dodgy.extend((hn0, hn1, hn2))
            d = Counter(dodgy).most_common()
            if dodgy:
                return get_dodgies(skippers=skippers + [d[0][0]])
            else:
                return skippers

        inter_mapping = {}
        for h1, h2 in itertools.combinations(hits, 2):
            inter_mapping[(h1.GetProp('_Name'), h2.GetProp('_Name'))] = self.get_positional_mapping(h1, h2)
        dodgy_names = get_dodgies([])
        warn(f'These combiend badly: {dodgy_names}')
        dodgies = [hit for hit in hits if hit.GetProp('_Name') in dodgy_names]
        mergituri = [hit for hit in hits if hit.GetProp('_Name') not in dodgy_names]
        merged = self.simply_merge_hits(mergituri)
        dodgies += [hit for hit in hits if hit.GetProp('_Name') in self.unmatched]
        self.unmatched = []
        combined_dodgies = []
        for h1, h2 in itertools.combinations(dodgies, 2):
            h_alt = Chem.Mol(h1)
            try:
                combined_dodgies.append(self.merge_pair(h_alt, h2))
            except ConnectionError:
                pass
        combinations = [merged] + dodgies + combined_dodgies
        # propagate alternatives
        while self.propagate_alternatives(combinations) != 0:
            pass
        return combinations

    def propagate_alternatives(self, fewer):
        pt = Chem.GetPeriodicTable()
        new = 0
        for template in list(fewer):
            for i, atom in enumerate(template.GetAtoms()):
                if atom.HasProp('_AltSymbol'):
                    alt = Chem.Mol(template)
                    aa = alt.GetAtomWithIdx(i)
                    aa.SetAtomicNum(pt.GetAtomicNumber(atom.GetProp('_AltSymbol')))
                    aa.ClearProp('_AltSymbol')
                    atom.ClearProp('_AltSymbol')
                    fewer.append(alt)
                    new += 1
        return new

    def pick_best(self) -> Tuple[Chem.Mol, int]:
        """
        Method for partial merging for placement

        :return: unrefined_scaffold, mode_index
        """
        if len(self.mol_options) == 1:
            return self.mol_options[0], 0
        elif len(self.mol_options) == 0:
            raise ValueError('No scaffolds made?!')
        else:
            mapx = {}  #: dictionary of key mol name and value tuple of maps and mode

            def template_sorter(t: List[Chem.Mol]) -> float:
                # key for sorting. requires outer scope ``maps``.
                self.journal.debug(f'pick_best (partial merging): Sorting {len(t)} templates: {t}')
                n_atoms = len(mapx[t[0].GetProp('_Name')])
                mode = mapx[t[1].GetProp('_Name')]
                mode_i = self.matching_modes.index(mode)
                return - n_atoms - mode_i / 10

            ## get data
            # presort as this is expensive.
            for template in self.mol_options:
                # _get_atom_maps returns a list of alternative mappings which are lists of template to initail mol
                atom_maps = self._get_atom_maps(template, self.initial_mol,
                                                atomCompare=rdFMCS.AtomCompare.CompareElements,
                                                bondCompare=rdFMCS.BondCompare.CompareOrder,
                                                ringMatchesRingOnly=True,
                                                ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                                                matchChiralTag=False)
                mapx[template.GetProp('_Name')] = (atom_maps, self.matching_modes[-1])
            # search properly only top 3.
            self.mol_options = sorted(self.mol_options, key=template_sorter)
            for template in self.mol_options[:3]:
                atom_map, mode = self.get_mcs_mapping(template, self.initial_mol)
                # get_mcs_mapping returns a dict going from template index to initial.
                mapx[template.GetProp('_Name')] = (atom_map, mode)
                self.journal.debug(f"With {template.GetProp('_Name')}, "+\
                                   "{len(atom_map)} atoms map using mode {self.matching_modes.index(mode)}")
            ## pick best template
            self.mol_options = sorted(self.mol_options, key=template_sorter)
            ## Check if missing atoms can be explained by a different one with no overlap
            best = self.mol_options[0]
            ## Fuse overlaps
            # best_map = maps[best.GetProp('_Name')][0]
            # full = set(range(self.initial_mol.GetNumAtoms()))
            # present = set(best_map.values())
            # missing = full - present
            # for other in self.mol_options:
            #     other_map = maps[other.GetProp('_Name')][0]
            #     found = set(other_map.values())
            #     if len(found) > 6 and len(present & found) == 0: # more than just a ring and no overlap
            #         fusion = self._fuse(best, other, best_map, other_map)
            return best, self.matching_modes.index(mapx[best.GetProp('_Name')][1])

    # def _fuse(self, mol_A: Chem.Mol, mol_B: Chem.Mol, map_A: Dict[int, int], map_B: Dict[int, int]) -> Chem.Mol:
    #     """
    #     Merge two compounds... but that are unlinked, using the followup as a guide.
    #     Conceptually different but overlapping is join_neighboring_mols
    #
    #     :param mol_A:
    #     :param mol_B:
    #     :param map_A:
    #     :param map_B:
    #     :return:
    #     """
    #     # No longer needed.
    #     fusion = Chem.RwMol(Chem.CombineMols(mol_A, mol_B))
    #     t = mol_A.GetNumAtoms()
    #     new_map_B = {k+t: v for k, v in map_B.items()}
    #     full = set(range(self.initial_mol.GetNumAtoms()))
    #     present_A = set(map_A.values())
    #     present_B = set(map_B.values())
    #
    #     def find_route(n):
    #         if n in present_A:
    #             return None
    #         elif n in present_B:
    #             return n
    #         else:
    #             path_raw = {m: find_route(m) for m in self.initial_mol.GetAtomWithIdx(n).GetNeighbors()}
    #             path = {i: path_raw[i] for i in path_raw if path_raw[i] is not None}
    #             if len(path) == 0:
    #                 return None
    #             else:
    #                 return {n: path}

    # ================= Chimera ========================================================================================

    def make_chimera(self, template: Chem.Mol, min_mode_index=0) -> Chem.Mol:
        """
        This is to avoid extreme corner corner cases.
        E.g. here the MCS is ringMatchesRingOnly=True and AtomCompare.CompareAny,
        while for the positioning this is not the case.

        Called by full and partial blending modes.

        :return:
        """
        # get the matches
        atom_map, mode = self.get_mcs_mapping(template, self.initial_mol, min_mode_index=min_mode_index)
        follow = {**{k: str(v) for k, v in mode.items()}, 'N_atoms': len(atom_map)}
        self.journal.debug(f"scaffold-followup: {follow}")
        # make the scaffold more like the followup to avoid weird matches.
        chimera = Chem.RWMol(template)
        for scaff_ai, follow_ai in atom_map.items():
            if template.GetAtomWithIdx(scaff_ai).GetSymbol() != self.initial_mol.GetAtomWithIdx(
                    follow_ai).GetSymbol():
                v = {'F': 1, 'Br': 1, 'Cl': 1, 'H': 1, 'B': 3, 'C': 4, 'N': 3, 'O': 2, 'S': 2, 'Se': 2, 'P': 6}
                wanted = self.initial_mol.GetAtomWithIdx(follow_ai)
                if wanted.GetSymbol() == '*':  # all good then!
                    continue
                owned = template.GetAtomWithIdx(scaff_ai)
                diff_valance = owned.GetExplicitValence() - v[wanted.GetSymbol()]
                if wanted.GetSymbol() in ('F', 'Br', 'Cl', 'C', 'H') and diff_valance > 0:
                    continue  # cannot change this.
                elif owned.GetExplicitValence() > 4 and wanted.GetSymbol() not in ('P',):
                    continue
                else:
                    newatom = Chem.Atom(wanted)
                    stdev = chimera.GetAtomWithIdx(scaff_ai).GetDoubleProp('_Stdev')
                    newatom.SetDoubleProp('_Stdev', stdev)
                    origin = chimera.GetAtomWithIdx(scaff_ai).GetProp('_Origin')
                    newatom.SetProp('_Origin', origin)
                    chimera.ReplaceAtom(scaff_ai, newatom)
                    if diff_valance > 0:
                        chimera.GetAtomWithIdx(scaff_ai).SetFormalCharge(diff_valance)
        try:
            chimera.UpdatePropertyCache()  # noqa
        except Chem.AtomValenceException as err:
            warn('Valance issue' + str(err))
        return chimera

    def place_from_map(self, target_mol: Chem.Mol, template_mol: Chem.Mol, atom_map: Optional[Dict] = None,
                       random_seed=None) -> Chem.Mol:
        """
        This method places the atoms with known mapping
        and places the 'uniques' (novel) via an aligned mol (the 'sextant')
        This sextant business is a workaround for the fact that only minimised molecules can use the partial
        embedding function of RDKit.

        The template molecule may be actually two or more fragments,
        as happens for the no blending mode.
        In RDKit, the fragments within a "molecule" are not connected, but have sequential atom indices.

        :param target_mol: target mol
        :param template_mol: the template/scaffold to place the mol
        :param atom_map: something that get_mcs_mapping would return.
        :return:
        """
        # Note none of this malarkey: AllChem.MMFFOptimizeMolecule(ref)
        # prealignment
        if target_mol is None:
            target_mol = self.initial_mol
        sextant = Chem.Mol(target_mol)
        # the target (initial) mol ought to be already sanitised.
        # TODO Check if this is necessary
        # Chem.SanitizeMol(sextant)
        if random_seed:
            kwargs = dict(randomSeed=random_seed)
        else:
            kwargs = {}
        AllChem.EmbedMolecule(sextant, **kwargs)
        AllChem.MMFFOptimizeMolecule(sextant)
        ######################################################
        # mapping retrieval and sextant alignment
        # variables: atom_map sextant -> uniques
        if atom_map is None:
            atom_map, mode = self.get_mcs_mapping(target_mol, template_mol)
            msg = {**{k: str(v) for k, v in mode.items()}, 'N_atoms': len(atom_map)}
            self.journal.debug(f"followup-chimera' = {msg}")
        else:
            self.journal.debug(f'Place from map: using provided atom_map {atom_map}')
        self._add_atom_map_asProp(template_mol, atom_map)
        rdMolAlign.AlignMol(sextant, template_mol, atomMap=list(atom_map.items()), maxIters=500)
        # place atoms that have a known location
        putty = Chem.Mol(sextant)  # this is the molecule returned
        pconf = putty.GetConformer()
        chimera_conf = template_mol.GetConformer()
        uniques = set()  # unique atoms in followup
        for i in range(putty.GetNumAtoms()):
            p_atom = putty.GetAtomWithIdx(i)
            p_atom.SetDoubleProp('_Stdev', 0.)
            p_atom.SetProp('_Origin', 'none')
            if i in atom_map:
                ci = atom_map[i]
                c_atom = template_mol.GetAtomWithIdx(ci)
                if c_atom.HasProp('_Stdev'):
                    stdev = c_atom.GetDoubleProp('_Stdev')
                    origin = c_atom.GetProp('_Origin')
                    p_atom.SetDoubleProp('_Stdev', stdev)
                    p_atom.SetProp('_Origin', origin)
                pconf.SetAtomPosition(i, chimera_conf.GetAtomPosition(ci))
            else:
                uniques.add(i)
        ######################################################
        # I be using a sextant for dead reckoning!
        # variables: sextant unique team
        categories = self._categorize(sextant, uniques)
        done_already = []  # multi-attachment issue.
        for unique_idx in categories['pairs']:  # attachment unique indices
            # check the index was not done already (by virtue of a second attachment)
            if unique_idx in done_already:
                continue
            # get other attachments if any.
            team = self._recruit_team(target_mol, unique_idx, categories['uniques'])
            other_attachments = (team & set(categories['pairs'].keys())) - {unique_idx}
            sights = set()  # atoms to align against
            for att_idx in [unique_idx] + list(other_attachments):
                for pd in categories['pairs'][att_idx]:
                    first_sight = pd['idx']
                    sights.add((first_sight, first_sight))
                    neighs = [i.GetIdx() for i in sextant.GetAtomWithIdx(first_sight).GetNeighbors() if
                              i.GetIdx() not in uniques]
                    for n in neighs:
                        sights.add((n, n))
            if self.attachment and list(categories['dummies']) and list(categories['dummies'])[0] in team:
                r = list(categories['dummies'])[0]
                pconf.SetAtomPosition(r, self.attachment.GetConformer().GetAtomPosition(0))
                sights.add((r, r))
            rdMolAlign.AlignMol(sextant, putty, atomMap=list(sights), maxIters=500)
            sconf = sextant.GetConformer()
            self.journal.debug(f'alignment atoms for {unique_idx} ({team}): {sights}')
            # self.draw_nicely(sextant, highlightAtoms=[a for a, b in sights])
            # copy position over
            for atom_idx in team:
                pconf.SetAtomPosition(atom_idx, sconf.GetAtomPosition(atom_idx))
            # the ring problem does not apply here but would result in rejiggling atoms.

            for other in other_attachments:
                done_already.append(other)
        # complete
        # Why is this performed: TODO Check if this is necessary
        # AllChem.SanitizeMol(putty)
        return putty  # positioned_mol



    def transfer_ring_data(self, donor: Chem.Atom, acceptor: Chem.Atom):
        """
        Transfer the info if a ringcore atom.

        :param donor:
        :param acceptor:
        :return:
        """
        # if donor.GetIntProp('_ori_i') == -1:
        #     data = donor
        pass

    # ========= Other ==================================================================================================

    def posthoc_refine(self, scaffold, indices: Optional[List[int]] = None) -> Chem.Mol:
        """
        Averages the overlapping atoms.

        :param scaffold:
        :return:
        """
        if indices is None:
            indices = list(range(scaffold.GetNumAtoms()))
        refined = Chem.RWMol(scaffold)
        refconf = refined.GetConformer()
        positions = defaultdict(list)  # coordinates
        equivalence = defaultdict(list)  # atom indices of hits.
        for h in self.hits:
            if h.GetProp('_Name') in self.unmatched:
                continue
            hc = h.GetConformer()
            for k, v in self.get_positional_mapping(scaffold, h).items():
                positions[k].append([hc.GetAtomPosition(v).x, hc.GetAtomPosition(v).y, hc.GetAtomPosition(v).z])
                equivalence[k].append(f'{h.GetProp("_Name")}.{v}')
        for i in range(scaffold.GetNumAtoms()):
            if i not in indices:
                continue
            elif len(positions[i]) == 0:
                refined.GetAtomWithIdx(i).SetDoubleProp('_Stdev', 0.)
                refined.GetAtomWithIdx(i).SetDoubleProp('_Max', 0.)
                refined.GetAtomWithIdx(i).SetProp('_Origin', 'none')
                # warn(f'Atom {i}  {scaffold.GetAtomWithIdx(i).GetSymbol}/{refined.GetAtomWithIdx(i).GetSymbol} '+ \
                #     'in scaffold that has no positions.')
            else:
                p = np.mean(np.array(positions[i]), axis=0).astype(float)
                # sd = np.mean(np.std(np.array(positions[i]), axis=0)).astype(float)
                ds = [np.linalg.norm(p - pi) for pi in positions[i]]
                sd = np.std(ds)
                md = np.max(ds)
                refined.GetAtomWithIdx(i).SetProp('_Origin', json.dumps(equivalence[i]))
                refined.GetAtomWithIdx(i).SetDoubleProp('_Stdev', sd)
                refined.GetAtomWithIdx(i).SetDoubleProp('_Max', md)
                if self.average_position:
                    refconf.SetAtomPosition(i, Point3D(p[0], p[1], p[2]))
        Chem.SanitizeMol(refined,
                         sanitizeOps=Chem.rdmolops.SanitizeFlags.SANITIZE_ADJUSTHS +
                                     Chem.rdmolops.SanitizeFlags.SANITIZE_SETAROMATICITY,
                         catchErrors=True)
        return refined

    def get_mcs_mappings(self, molA, molB, min_mode_index: int = 0) -> Tuple[List[Dict[int, int]], dict]:
        """
        This is a weird method. It does a strict MCS match.
        And then it uses laxer searches and finds the case where a lax search includes the strict search.

        :param molA: query molecule
        :param molB: target/ref molecule
        :param min_mode_index: the lowest index to try (opt. speed reasons)
        :return: mappings and mode
        """
        strict = self._get_atom_maps(molA, molB, **self.strict_matching_mode)
        for i, mode in enumerate(self.matching_modes):
            if i < min_mode_index:
                continue
            # heme is 70 or so atoms & Any-Any matching gets stuck. So guestimate to avoid that:
            if molA.GetNumAtoms() > (50 + i * 10) or molB.GetNumAtoms() > (50 + i * 10):
                continue
            lax = self._get_atom_maps(molA, molB, **mode)
            # remove the lax matches that disobey
            neolax = [l for l in lax if any([len(set(s) - set(l)) == 0 for s in strict])]
            if len(neolax) == 0:
                continue
            else:
                return [dict(n) for n in neolax], mode
        else:
            # Then the strict will have to do.
            return [dict(n) for n in strict], strict_settings  # tuple to dict
            # raise ValueError('This is chemically impossible: nothing matches in the MCS step ' +\
            #                  f'({len(self.matching_modes)} modes tried')

    def get_mcs_mapping(self, molA, molB, min_mode_index: int = 0) -> Tuple[Dict[int, int], dict]:
        """
        This is a weird method. It does a strict MCS match.
        And then it uses laxer searches and finds the case where a lax search includes the strict search.

        :param molA: query molecule
        :param molB: target/ref molecule
        :param min_mode_index: the lowest index to try (opt. speed reasons)
        :return: mapping and mode
        """
        ms, mode = self.get_mcs_mappings(molA, molB, min_mode_index)
        return ms[0], mode

    def _get_atom_maps(self, molA, molB, **mode) -> Set[Tuple[Tuple[int, int]]]:
        mcs = rdFMCS.FindMCS([molA, molB], **mode)
        common = Chem.MolFromSmarts(mcs.smartsString)
        matches = []
        # prevent a dummy to match a non-dummy, which can happen when the mode is super lax.
        is_dummy = lambda mol, at: mol.GetAtomWithIdx(at).GetSymbol() == '*'
        def all_bar_dummy(Aat, Bat) -> bool:
            """it is okay to match a dummy with dummy only"""
            a_dummy:bool = is_dummy(molA, Aat)
            b_dummy:bool = is_dummy(molB, Bat)
            # xor
            return (a_dummy and b_dummy) or not (a_dummy or b_dummy)
        # prevent matching hydrogens
        is_hydrogen = lambda mol, at: mol.GetAtomWithIdx(at).GetSymbol() == 'H'
        for molA_match in molA.GetSubstructMatches(common, uniquify=False):
            for molB_match in molB.GetSubstructMatches(common, uniquify=False):
                matches.append([(int(molA_at), int(molB_at)) for molA_at, molB_at in zip(molA_match, molB_match) if
                                all_bar_dummy(molA_at, molB_at) and
                                not is_hydrogen(molA, molA_at) and
                                not is_hydrogen(molB, molB_at)])
        # you can map two toluenes 4 ways, but two are repeats.
        matches = set([tuple(sorted(m, key=lambda i: i[0])) for m in matches])
        return matches

    def _get_atom_map(self, molA, molB, **mode) -> List[Tuple[int, int]]:
        return self._get_atom_maps(molA, molB, **mode)[0]

    def pretweak(self) -> None:
        """
        What if the fragments were prealigned slightly? Really bad things.

        :return:
        """
        warn('This method is unreliable. Do not use it')
        ref = self.hits[0]
        for target in self.hits[1:]:
            A2B = list(self.get_positional_mapping(target, ref, 0.5).items())
            if A2B:
                rdMolAlign.AlignMol(target, ref, atomMap=A2B, maxIters=500)
            else:
                warn(f'No overlap? {A2B}')

    @property
    def matched(self) -> List[str]:
        """
        This is the counter to unmatched.
        It's dynamic as you never know...

        :return:
        """
        return [h.GetProp('_Name') for h in self.hits if
                h.GetProp('_Name') not in self.unmatched]


    def sample_new_conformation(self, random_seed=None):
        scaffold = self.modifications["chimera"]
        atom_map = self.get_atom_map_fromProp(scaffold)
        if random_seed is None:
            random_seed = self.random_seed
        new_mol = self.place_from_map(target_mol=Chem.Mol(self.initial_mol), template_mol=scaffold, atom_map=atom_map,
                       random_seed=random_seed)

        merging_mode = getattr(self, "merging_mode", "off")
        if merging_mode == 'off':
            pass
        elif merging_mode == 'full':
            pass
        elif merging_mode == 'partial':
            self.partial_blending()
        elif merging_mode == 'none_permissive' or merging_mode == 'permissive_none' or \
            merging_mode == 'none':
            new_mol = self.posthoc_refine(new_mol)
        else:
            valid_modes = ('full', 'partial', 'none', 'none_permissive', 'off')
            raise ValueError(
                f"Merging mode can only be {'| '.join(valid_modes)}, not '{merging_mode}'")

        self.positioned_mol = new_mol
        return new_mol

    @classmethod
    def get_best_scoring(cls, mols: List[Chem.RWMol]) -> Chem.Mol:
        """
        Sorts molecules by how well they score w/ Merch FF
        """
        if len(mols) == 0:
            raise ValueError(f'No molecules')
        elif len(mols) == 1:
            return mols[0]
        # This is not expected to happen, but just in case
        mols = [m for m in mols if m is not None]
        scores = *map(cls.score_mol, mols),
        cls.journal.debug(f'`.get_best_scoring (unmerge)` Scores: {scores}')
        # proof that the mol has/lacks origins data:
        # for mol in mols:
        #     print('DEBUG OF LAST RESORT', mol)
        #     print(cls.origin_from_mol(cls, mol.GetMol())) # called from instance
        mol_scores = sorted(list(zip(mols, scores)), key=operator.itemgetter(1))
        return mol_scores[0][0]

    @staticmethod
    def score_mol(mol: Chem.Mol) -> float:
        """
        Scores a mol without minimising
        """
        if isinstance(mol, Chem.RWMol):
            mol = mol.GetMol()
        else:
            mol = Chem.Mol(mol)
        mol.UpdatePropertyCache()  # noqa
        Chem.SanitizeMol(mol)
        p = AllChem.MMFFGetMoleculeProperties(mol, 'MMFF94')
        if p is None:
            return float('nan')
        ff = AllChem.MMFFGetMoleculeForceField(mol, p)
        return ff.CalcEnergy()
