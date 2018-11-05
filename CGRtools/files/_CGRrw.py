# -*- coding: utf-8 -*-
#
#  Copyright 2014-2018 Ramil Nugmanov <stsouko@live.ru>
#  This file is part of CGRtools.
#
#  CGRtools is free software; you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
from abc import abstractmethod
from collections import defaultdict
from itertools import count, chain
from io import StringIO, BytesIO, TextIOWrapper
from pathlib import Path
from warnings import warn
from ..containers import ReactionContainer, MoleculeContainer, CGRContainer, QueryContainer
from ..exceptions import InvalidStereo, InvalidAtom, InvalidConfig, MapError, InvalidData
from ..periodictable import elements_set
from ..periodictable import common_isotopes


fromMDL = (0, 3, 2, 1, 0, -1, -2, -3)


class WithMixin:
    def __init__(self, file, mode='r'):
        if mode not in ('r', 'w', 'rb'):
            raise InvalidConfig('invalid mode')
        if not file:
            raise InvalidConfig('invalid file')
        if isinstance(file, str):
            self._file = open(file, mode)
            self.__is_buffer = False
        elif isinstance(file, Path):
            self._file = file.open(mode)
            self.__is_buffer = False
        elif isinstance(file, StringIO) and mode in 'rw':
            self._file = file
        elif isinstance(file, TextIOWrapper) and mode == 'r':
            self._file = file
        elif isinstance(file, BytesIO) and mode == 'rb':
            self._file = file
        elif hasattr(file, 'read') and file.mode == mode:  # check if file is open(filename, mode)
            self._file = file
        else:
            raise InvalidConfig('invalid file')
        self.__write = mode == 'w'

    def close(self, force=False):
        if self.__write:
            self.write = self.__write_adhoc
            self.__write = False

        if not self.__is_buffer or force:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, value, traceback):
        self.close()

    @staticmethod
    def __write_adhoc(_):
        raise ValueError('I/O operation on closed writer')

    __is_buffer = True


class CGRread:
    def __init__(self, remap=True, ignore=False, is_template=False, colors=None):
        self.__remap = remap
        self.__ignore = ignore
        self.__is_template = is_template
        self.__colors = colors

    def _get_reaction(self, reaction):
        spmaps = None
        maps = {}
        for i in ('reagents', 'products', 'reactants'):
            maps[i] = tmp = []
            for m in reaction[i]:
                mm = []
                for a in m['atoms']:
                    am = a['map']
                    if not self.__ignore and am and am in mm:
                        raise MapError('mapping in molecules should be unique')
                    else:
                        mm.append(am)
                    tmp.append(am)

        length = count(max(max(maps['products'], default=0), max(maps['reagents'], default=0),
                           max(maps['reactants'], default=0)) + 1)

        ''' map unmapped atoms.
        '''
        for i in ('reagents', 'products', 'reactants'):
            mi, used = [], []
            for m in maps[i]:
                k = m or next(length)
                if k in used:
                    if not self.__ignore:
                        raise MapError('mapping in reagents or products or reactants should be unique')
                    # force remap non unique atoms in molecules.
                    mi.append((next(length), k))
                else:
                    mi.append((k, k))
                    used.append(k)

            maps[i] = mi

        if maps['reactants']:
            spmaps = set(x for x, _ in chain(maps['reagents'], maps['products']))
            d = set(x for x, _ in maps['reactants']).intersection(spmaps)
            if d:
                if not self.__ignore:
                    raise MapError('reactants has map intersection with reagents or products')
                maps['reactants'] = [(x if x not in d else next(length), y) for x, y in maps['reactants']]

        ''' find breaks in map. e.g. 1,2,5,6. 3,4 - skipped
        '''
        if self.__remap:
            lose = sorted(set(range(1, next(length)))
                          .difference(spmaps or (x for x, _ in chain(maps['reagents'], maps['products'])))
                          .difference(x for x, _ in maps['reactants']), reverse=True)
            if lose:
                for i in ('reagents', 'products', 'reactants'):
                    if not maps[i]:
                        continue
                    for j in lose:
                        maps[i] = [(x if x < j else x - 1, y) for x, y in maps[i]]
        ''' end
        '''
        rc = ReactionContainer(meta={x: '\n'.join(y) for x, y in reaction['meta'].items()})

        colors = {}
        if self.__colors:
            for k, v in reaction['meta'].items():
                if k.startswith(self.__colors):
                    try:
                        mol_num = k.split('.', 1)[1]
                        colors[int(mol_num)] = v
                    except (ValueError, IndexError):
                        warn(f'coloring data invalid: {k}, {v}', ResourceWarning)

        counter = count(1)
        for i in ('reagents', 'products', 'reactants'):
            shift = 0
            for j in reaction[i]:
                atom_len = len(j['atoms'])
                remapped = {x: y for x, y in enumerate(maps[i][shift: atom_len + shift], start=1)}
                shift += atom_len
                g = self.__parse_molecule(j, remapped, colors=colors.get(next(counter)))
                rc[i].append(g)
        return rc

    def _get_molecule(self, molecule):
        if self.__remap:
            remapped = {k: (k, k) for k in range(1, len(molecule['atoms']) + 1)}
        else:
            length = count(max(x['map'] for x in molecule['atoms']) + 1)
            remapped, used = {}, []
            for k, l in enumerate(molecule['atoms'], start=1):
                m = l['map'] or next(length)
                if m in used:
                    if not self.__ignore:
                        raise MapError('mapping in molecules should be unique')

                    remapped[k] = (next(length), m)
                else:
                    remapped[k] = (m, m)
                    used.append(m)

        colors = molecule['meta'].get(self.__colors) if self.__colors else None
        return self.__parse_molecule(molecule, remapped, meta=molecule['meta'], colors=colors)

    @classmethod
    def __parsedyn(cls, name, value):
        tmp0, tmp1 = [], []
        for x in value.split(','):
            s, *_, p = x.split('>')
            if s != p:
                if name == 'order':
                    tmp0.append(cls.__bondlabels[s])
                    tmp1.append(cls.__bondlabels[p])
                else:
                    tmp0.append(s != 'n' and int(s) or None)
                    tmp1.append(p != 'n' and int(p) or None)

        p_name = f'p_{name}'
        if len(tmp0) == 1:
            out = {}
            if tmp0[0] is not None:
                out[name] = tmp0[0]
            if tmp1[0] is not None:
                out[p_name] = tmp1[0]
            return out, False
        elif len(tmp0) > 1:
            tmp2 = list(zip(tmp0, tmp1))
            if len(set(tmp2)) == len(tmp2):
                return {name: tmp0, p_name: tmp1}, True
        warn(f'CGR data invalid: {name}, {value}', ResourceWarning)

    @classmethod
    def __parselist(cls, name, value):
        if name == 'order':
            tmp = [cls.__bondlabels[x] for x in value.split(',')]
        else:
            tmp = [(x != 'n' and int(x) or None) for x in value.split(',')]

        p_name = f'p_{name}'
        if len(tmp) == 1:
            if tmp[0] is not None:
                return {name: tmp[0], p_name: tmp[0]}, False
        elif len(set(tmp)) == len(tmp) and None not in tmp:
            return {name: tmp, p_name: tmp}, True
        warn(f'CGR data invalid: {name}, {value}', ResourceWarning)

    @classmethod
    def __parse_cgr_atom(cls, value, base, name):
        p_name = f'p_{name}'
        diff = [int(x) for x in value]
        if len(diff) > 1:
            if diff[0] == 0:  # for list of charges c0,1,-2,+3...
                tmp = [base + x for x in diff]
                if len(set(tmp)) == len(tmp):
                    return {name: tmp, p_name: tmp}, True
            elif len(diff) % 2 == 1:  # for dyn charges c1,-1,0... -1,0 is dyn group relatively to base
                s = [base] + [base + x for x in diff[1::2]]
                p = [base + x for x in diff[::2]]
                tmp = list(zip(s, p))
                if len(set(tmp)) == len(tmp):
                    return {name: s, p_name: p}, True
        else:
            tmp = diff[0]
            if tmp:
                p = base + tmp
                return {name: base, p_name: p}, False

    @classmethod
    def __cgr_dat(cls, _type, value):
        if _type == 'atomlist':
            if set(value).difference(elements_set):
                raise InvalidAtom('atom list contain invalid atoms')
            return dict(element=value), True

        elif _type == 'atomnotlist':
            if set(value).difference(elements_set):
                raise InvalidAtom('atom not list contain invalid atoms')
            return dict(element=list(elements_set.difference(value))), True

        elif _type == 'atomhyb':
            return cls.__parselist('hyb', value)

        elif _type == 'atomneighbors':
            return cls.__parselist('neighbors', value)

        elif _type == 'dynatomhyb':
            return cls.__parsedyn('hyb', value)

        elif _type == 'dynatomneighbors':
            return cls.__parsedyn('neighbors', value)

    @staticmethod
    def __parse_colors(colors):
        adhoc, before, res = [], [], defaultdict(dict)
        for x in colors:
            if (len(x) == 81 or len(x) == 75 and not before) and x[-1] == '+':
                before.append(x[:-1])
            else:
                before.append(x)
                adhoc.append(''.join(before).split())
                before = []

        for population, *keys in adhoc:
            p = int(population)
            for x in keys:
                atom, val = x.split(':')
                a = int(atom)
                res[a][p] = val
        return res

    def __parse_molecule(self, molecule, mapping, meta=None, colors=None):
        meta = meta and {x: '\n'.join(y) for x, y in meta.items()} or None
        cgr_dat_atom, cgr_dat_bond, cgr_dat_stereo, isotope_dat = defaultdict(dict), defaultdict(dict), {}, {}
        any_bonds, normal_stereo = [], []
        charge_dat = {k['atoms'][0]: int(k['value']) for k in molecule['CGR_DAT'] if k['type'] == 'charge'}
        radical_dat = {k['atoms'][0]: int(k['value']) for k in molecule['CGR_DAT'] if k['type'] == 'radical'}
        colors = self.__parse_colors(colors) if colors else {}
        is_query = self.__is_template

        for k in molecule['CGR_DAT']:
            k_atoms = k['atoms']
            k_type = k['type']
            if len(k_atoms) != self._cgr_keys.get(k_type):
                raise InvalidData('CGR data invalid. invalid number of atoms')
            if not k_atoms[0] or len(k_atoms) == 2 and not k_atoms[1]:
                raise InvalidData('CGR data invalid. contains zero atoms')

            if k_type in ('charge', 'radical'):
                continue
            elif k_type == 'dynatom':
                key = k['value'][0]
                value = k['value'][1:].split(',')
                a1 = k_atoms[0]
                if key == 'x':
                    x, y, z = (float(x) for x in value)
                    cgr_dat_atom[a1].update(p_x=x, p_y=y, p_z=z)
                else:
                    if key == 'c':
                        val = self.__parse_cgr_atom(value, charge_dat.get(a1, molecule['atoms'][a1 - 1]['charge']),
                                                    'charge')
                        if val:
                            cgr_dat_atom[a1].update(val[0])
                            if val[1] and not is_query:
                                is_query = True
                        else:
                            warn(f'dynatom charge invalid data. skipped: {value}', ResourceWarning)
                    elif key == 'r':
                        val = self.__parse_cgr_atom(value, radical_dat.get(a1, 0), 'multiplicity')
                        if val:
                            cgr_dat_atom[a1].update(val[0])
                            if val[1] and not is_query:
                                is_query = True
                        else:
                            warn(f'dynatom radical invalid data. skipped: {value}', ResourceWarning)
            elif k_type == 'dynstereo':
                s_stereo, p_stereo = k['value'].split('>')
                cgr_dat_stereo[k_atoms] = (self.__stereolabels[s_stereo], self.__stereolabels[p_stereo])
            elif k_type == 'extrabond':
                val = self.__parselist('order', k['value'])
                if val:
                    a1, a2 = k_atoms
                    cgr_dat_bond[a1][a2] = cgr_dat_bond[a2][a1] = val[0]
                    if val[1] and not is_query:
                        is_query = True
            elif k_type == 'dynbond':
                val = self.__parsedyn('order', k['value'])
                if val:
                    a1, a2 = k_atoms
                    cgr_dat_bond[a1][a2] = cgr_dat_bond[a2][a1] = val[0]
                    if val[1] and not is_query:
                        is_query = True
            elif k_type == 'isotope':
                tmp = k['value'].split(',')
                if len(tmp) > 1:
                    cgr_dat_atom[k_atoms[0]]['isotope'] = [int(x) for x in tmp]
                    if not is_query:
                        is_query = True
                else:
                    isotope_dat[k_atoms[0]] = int(tmp[0])
            else:
                val = self.__cgr_dat(k_type, k['value'])
                if val:
                    cgr_dat_atom[k_atoms[0]].update(val[0])
                    if val[1] and not is_query:
                        is_query = True

        if not is_query:
            is_query = any(x['element'] in ('A', '*') for x in molecule['atoms'])
        is_cgr = is_query or bool(cgr_dat_atom or cgr_dat_bond or cgr_dat_stereo)

        if is_query:
            g = QueryContainer()
        elif is_cgr:
            g = CGRContainer()
        else:
            g = MoleculeContainer()
        if meta:
            g.meta.update(meta)

        for k, l in enumerate(molecule['atoms'], start=1):
            atom_map, parsed_map = mapping[k]
            element = l['element']
            if element == 'D':
                element = 'H'
                isotope_dat[k] = 2
            elif element == 'T':
                element = 'H'
                isotope_dat[k] = 3

            if is_cgr:
                if k in cgr_dat_atom:
                    atom_dat = cgr_dat_atom[k]
                    if 'element' not in atom_dat:
                        atom_dat['element'] = element
                    if 'charge' not in atom_dat:
                        lc = charge_dat.get(k, l['charge'])
                        atom_dat.update(charge=lc, p_charge=lc)
                    if 'multiplicity' not in atom_dat:
                        lr = radical_dat.get(k)
                        atom_dat.update(multiplicity=lr, p_multiplicity=lr)
                    if 'p_x' in atom_dat:
                        atom_dat['p_x'] += l['x']
                        atom_dat['p_y'] += l['y']
                        atom_dat['p_z'] += l['z']
                    else:
                        atom_dat.update(p_x=l['x'], p_y=l['y'], p_z=l['z'])
                    if k in colors:
                        atom_dat['color'] = colors[k]
                    if 'isotope' not in atom_dat:
                        if k in isotope_dat:
                            atom_dat['isotope'] = isotope_dat[k]
                        elif l['isotope']:
                            atom_dat['isotope'] = common_isotopes[element] + l['isotope']

                    g.add_node(atom_map, mark=l['mark'], mapping=parsed_map, x=l['x'], y=l['y'], z=l['z'], **atom_dat)
                else:
                    lc = charge_dat.get(k, l['charge'])
                    lr = radical_dat.get(k)
                    g.add_node(atom_map, element=element, mark=l['mark'], mapping=parsed_map,
                               isotope=isotope_dat.get(k) or common_isotopes[element] + l['isotope'],
                               charge=lc, p_charge=lc, multiplicity=lr, p_multiplicity=lr,
                               x=l['x'], y=l['y'], z=l['z'], p_x=l['x'], p_y=l['y'], p_z=l['z'], color=colors.get(k))
            else:
                g.add_node(atom_map, element=element, mark=l['mark'], mapping=parsed_map,
                           isotope=isotope_dat.get(k) or common_isotopes[element] + l['isotope'],
                           color=colors.get(k), charge=charge_dat.get(k, l['charge']),
                           multiplicity=radical_dat.get(k), x=l['x'], y=l['y'], z=l['z'])

        for k, l, m, s in molecule['bonds']:
            k_map, l_map = mapping[k][0], mapping[l][0]
            if is_cgr:
                if m == 8 and k in cgr_dat_bond and l in cgr_dat_bond[k]:  # dynbond only for any bond accept.
                    g.add_edge(k_map, l_map, **cgr_dat_bond[k][l])
                else:
                    g.add_edge(k_map, l_map, order=m, p_order=m)
                if m == 8:
                    any_bonds.append((k, l))
                    any_bonds.append((l, k))
            else:
                g.add_edge(k_map, l_map, order=m)

            if s in (1, 6) and m in (1, 4):
                s_mark = self.__stereolabels[s]
                normal_stereo.append((k_map, l_map, s_mark))

        while True:
            failed_cgr_dat_stereo = {}
            for (k, l), (s_mark, p_mark) in cgr_dat_stereo.items():
                if (k, l) in any_bonds:  # remove dynamic stereo if bond not any [8]. INVALID SPEC.
                    k_map, l_map = mapping[k][0], mapping[l][0]
                    try:
                        g.add_stereo(k_map, l_map, s_mark, p_mark)
                    except InvalidStereo as e:
                        warn(str(e), ResourceWarning)
                        failed_cgr_dat_stereo[(k, l)] = (s_mark, p_mark)
                    except InvalidAtom as e:
                        if not self.__ignore:
                            raise
                        warn(str(e), ResourceWarning)

            failed_normal_stereo = []
            for k_map, l_map, s_mark in normal_stereo:
                try:
                    if is_cgr:
                        g.add_stereo(k_map, l_map, s_mark, s_mark)
                    else:
                        g.add_stereo(k_map, l_map, s_mark)
                except InvalidStereo as e:
                    warn(str(e), ResourceWarning)
                    failed_normal_stereo.append((k_map, l_map, s_mark))
                except InvalidAtom as e:
                    if not self.__ignore:
                        raise
                    warn(str(e), ResourceWarning)

            if failed_cgr_dat_stereo and len(cgr_dat_stereo) > len(failed_cgr_dat_stereo) or \
                    failed_normal_stereo and len(normal_stereo) > len(failed_normal_stereo):
                cgr_dat_stereo = failed_cgr_dat_stereo
                normal_stereo = failed_normal_stereo
                continue
            break

        return g

    __marks = {mark: ('s_%s' % mark, 'p_%s' % mark, 'sp_%s' % mark) for mark in ('neighbors', 'hyb', 'bond')}
    __bondlabels = {'0': None, '1': 1, '2': 2, '3': 3, '4': 4, '9': 9, 'n': None, 's': 9}
    __stereolabels = {'0': None, '1': 1, '-1': -1, '+1': 1, 'n': None, 1: 1, 6: -1, -1: -1}
    _cgr_keys = dict(extrabond=2, dynbond=2, dynatom=1, atomnotlist=1, atomlist=1, isotope=1, charge=1, radical=1,
                     atomhyb=1, atomneighbors=1, dynatomhyb=1, dynatomneighbors=1, dynstereo=2)


class CGRwrite:
    def __init__(self, extralabels=False, mark_to_map=False, xyz=False, fix_position=True, colors='ISIDA_COLORS'):
        self.__xyz = xyz
        self.__mark_to_map = mark_to_map
        self.__extralabels = extralabels
        self._fix_position = fix_position
        self.__colors = colors

    def get_formatted_cgr(self, g, shift=0):
        is_cgr = isinstance(g, (CGRContainer, QueryContainer))
        s_x = [x.x for x in g._node.values()]
        s_y = [x.y for x in g._node.values()]
        y_shift = -(max(s_y) + min(s_y)) / 2
        min_x, max_x = min(s_x), max(s_x)
        x_shift = shift - min_x
        data = dict(meta=g.meta.copy(), y_shift=y_shift)
        if self._fix_position:
            data.update(max_x=max_x + x_shift, min_x=shift)
        else:
            data.update(max_x=max_x, min_x=min_x)

        cgr_dat, extended, atoms, bonds = [], [], [], []
        renum, colors = {}, defaultdict(list)
        for n, (i, l) in enumerate(g._node.items(), start=1):
            renum[i] = n

            if is_cgr:
                charge, meta = self.__dyn_atom(l.charge, l.p_charge, 'c')
                if meta:
                    meta['atoms'] = (n,)
                    cgr_dat.append(meta)

                radical, meta = self.__dyn_atom(l.multiplicity, l.p_multiplicity, 'r')
                if meta:
                    meta['atoms'] = (n,)
                    cgr_dat.append(meta)
            else:
                charge = self._charge_map[l.charge]
                radical = self._radical_map[l.multiplicity]

            if radical:
                extended.append(dict(atom=n, value=radical, type='radical'))

            if self.__extralabels:
                if is_cgr:
                    meta = self.__get_state(l.hybridization, l.p_hybridization, 'atomhyb', 'dynatomhyb')
                    if meta:
                        meta['atoms'] = (n,)
                        cgr_dat.append(meta)
                    meta = self.__get_state(l.neighbors, l.p_neighbors, 'atomneighbors', 'dynatomneighbors')
                    if meta:
                        meta['atoms'] = (n,)
                        cgr_dat.append(meta)
                else:
                    if l.hybridization:
                        cgr_dat.append({'atoms': (n,), 'type': 'atomhyb', 'value': l.hybridization})
                    if l.neighbors:
                        cgr_dat.append({'atoms': (n,), 'type': 'atomneighbors', 'value': l.neighbors})

            if l.isotope != l.common_isotope:
                if isinstance(l.isotope, list):
                    cgr_dat.append(dict(atoms=(n,), value=','.join(str(x) for x in l.isotope), type='isotope'))
                else:
                    extended.append(dict(atom=n, value=l.isotope, type='isotope'))

            l_colors = l.colors
            if l_colors:
                if is_cgr:
                    l_p_colors = l.p_colors
                    for part, s_val in l_colors.items():
                        p_val = l_p_colors[part]
                        if s_val != p_val:
                            val = f'{n}:{s_val}>{p_val}'
                        else:
                            val = f'{n}:{s_val}'
                        colors[part].append(val)
                else:
                    for part, s_val in l_colors.items():
                        colors[part].append(f'{n}:{s_val}')

            element = l.element
            if isinstance(element, list):
                extended.append(dict(atom=n, value=element, type='atomlist'))
                element = 'L'

            if self.__xyz and is_cgr:
                dx, dy, dz = l.p_x - l.x, l.p_y - l.y, l.p_z - l.z
                if abs(dx) > .0001 or abs(dy) > .0001 or abs(dz) > .0001:
                    cgr_dat.append(dict(atoms=(n,), value='x%.4f,%.4f,%.4f' % (dx, dy, dz), type='dynatom'))

            x, y = l.x, l.y
            if self._fix_position:
                x += x_shift
                y += y_shift
            atoms.append(dict(map=l.mark if self.__mark_to_map else i, charge=charge,
                              element=element, mark=l.mark, x=x, y=y, z=l.z))

        data['colors'] = {self.__colors: '\n'.join(f'%s %s' % (x, ' '.join(y)) for x, y in colors.items())}

        for i, j, l in g.edges(data=True):
            if is_cgr:
                tmp = g.get_stereo(i, j)
                if tmp:
                    s_stereo, p_stereo = tmp
                else:
                    tmp = g.get_stereo(j, i)
                    if tmp:
                        s_stereo, p_stereo = tmp
                        i, j = j, i
                    else:
                        s_stereo = p_stereo = None
            else:
                s_stereo = g.get_stereo(i, j)
                if s_stereo:
                    p_stereo = s_stereo
                else:
                    s_stereo = p_stereo = g.get_stereo(j, i)
                    if s_stereo:
                        i, j = j, i

            re_atoms = (renum[i], renum[j])
            if s_stereo or p_stereo:
                stereo = {'value': '%s>%s' % (s_stereo or 'n', p_stereo or 'n'), 'type': 'dynstereo', 'atoms': re_atoms}
                if s_stereo != p_stereo:
                    s_mark = 0
                    s_dyn = True
                else:
                    s_mark = self._stereo_map[s_stereo]
                    s_dyn = False
            else:
                stereo = False
                s_mark = 0
                s_dyn = False

            s_bond = l.order or 0
            p_bond = l.p_order or 0 if is_cgr else s_bond
            if isinstance(s_bond, list):
                if s_bond == p_bond:
                    cgr_dat.append({'value': ','.join(s_bond), 'type': 'extrabond', 'atoms': re_atoms})
                else:
                    cgr_dat.append({'value': ','.join('%s>%s' % x for x in zip(s_bond, p_bond)), 'type': 'dynbond',
                                    'atoms': re_atoms})
                if stereo:
                    cgr_dat.append(stereo)
                bond = 8
            elif s_bond != p_bond:
                cgr_dat.append({'value': '%s>%s' % (s_bond, p_bond), 'type': 'dynbond', 'atoms': re_atoms})
                if stereo:
                    cgr_dat.append(stereo)
                bond = 8
            elif s_bond == 9:
                cgr_dat.append({'value': 's', 'type': 'extrabond', 'atoms': re_atoms})
                if stereo:
                    cgr_dat.append(stereo)
                bond = 8
            elif s_dyn:
                cgr_dat.append({'value': s_bond, 'type': 'extrabond', 'atoms': re_atoms})
                cgr_dat.append(stereo)
                bond = 8
            else:
                bond = s_bond

            bonds.append((*re_atoms, bond, s_mark))

        data['CGR'] = self._format_mol(atoms, bonds, extended, cgr_dat)
        return data

    @classmethod
    @abstractmethod
    def _format_mol(cls, atoms, bonds, extended, cgr_dat):
        pass

    @staticmethod
    def __get_state(s, p, t1, t2):
        if isinstance(s, list):
            if s == p:
                _val = ','.join(x and str(x) or 'n' for x in s)
                _type = t1
            else:
                _val = ','.join('%s>%s' % (x or 'n', y or 'n') for x, y in zip(s, p) if x != y)
                _type = t2

        elif s != p:
            _val = '%s>%s' % (s or 'n', p or 'n')
            _type = t2
        else:
            _val = s
            _type = t1

        return _val and {'value': _val, 'type': _type}

    def __dyn_atom(self, s, p, mark):
        if isinstance(s, list):
            value = s[0]
            if s == p:
                meta = dict(type='dynatom',
                            value='%s0%s' % (mark, ','.join(str(x) for x in s[1:]))) if len(s) > 1 else None
            elif s[0] != p[0]:
                meta = dict(type='dynatom',
                            value='%s%s' % (mark, ','.join(chain(('%+d' % (p[0] - value),),
                                                                 ('%+d,%+d' % (x - value, y - value) for x, y in
                                                                  zip(s[1:], p[1:]) if x != y)))))
            else:
                meta = None
        elif p != s:
            if p is None:
                p = 0
            if s is None:
                s = 0
            meta = dict(value='%s%+d' % (mark, (p - s)), type='dynatom')
            value = s
        else:
            meta = None
            value = s
        res = self._charge_map[value] if mark == 'c' else self._radical_map[value]
        return res, meta

    @staticmethod
    def _get_position(cord):
        if len(cord) > 1:
            x = (cord[-1]['x'] + cord[0]['x']) / 2 + .2
            y = (cord[-1]['y'] + cord[0]['y']) / 2
            dy = cord[-1]['y'] - cord[0]['y']
            dx = cord[-1]['x'] - cord[0]['x']
            if dx > 0:
                y += -.2 if dy > 0 else .2
            elif dx < 0:
                y += -.2 if dy < 0 else .2
        else:
            x, y = cord[0]['x'] + .25, cord[0]['y']

        return x, y

    @property
    @abstractmethod
    def _stereo_map(self):
        pass

    @property
    @abstractmethod
    def _charge_map(self):
        pass

    @property
    @abstractmethod
    def _radical_map(self):
        pass

    _half_table = len(elements_set) // 2
