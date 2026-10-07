"""Read-only evidence for UE4.18's Windows UpdateResource staging operation."""
import hashlib
import struct


def _pe(data):
    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError('Truncated PE structure')
        return struct.unpack_from(fmt, data, offset)

    if data[:2] != b'MZ':
        raise ValueError('Invalid DOS signature')
    pe, = unpack('<I', 60)
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid PE signature')
    machine, count = unpack('<HH', pe + 4)
    optional_size, characteristics = unpack('<HH', pe + 20)
    optional = pe + 24
    if (machine != 0x8664 or characteristics & 0x2000 or count < 1 or count > 96
            or optional_size < 240 or unpack('<H', optional)[0] != 0x20B
            or unpack('<I', optional + 108)[0] < 3):
        raise ValueError('Expected a Win64 PE32+ executable with resources')
    table = optional + optional_size
    header_size, = unpack('<I', optional + 60)
    alignment, = unpack('<I', optional + 36)
    if (not alignment or alignment & (alignment - 1)
            or table + count * 40 > header_size or header_size > len(data)):
        raise ValueError('Invalid PE header layout')
    sections = []
    for index in range(count):
        position = table + index * 40
        name = data[position:position + 8]
        virtual_size, rva, raw_size, offset = unpack('<IIII', position + 8)
        flags, = unpack('<I', position + 36)
        if raw_size and (offset < header_size or offset + raw_size > len(data)):
            raise ValueError('PE section lies outside the file')
        sections.append(dict(name=name, virtual_size=virtual_size, rva=rva,
                             raw_size=raw_size, offset=offset, flags=flags, position=position))
    spans = sorted((s['offset'], s['offset'] + s['raw_size']) for s in sections if s['raw_size'])
    if any(start < previous_end for (_, previous_end), (start, _) in zip(spans, spans[1:])):
        raise ValueError('Overlapping PE sections')
    resources = [s for s in sections if s['name'].rstrip(b'\0') == b'.rsrc']
    if len(resources) != 1:
        raise ValueError('Expected exactly one resource section')
    resource = resources[0]
    resource_rva, resource_size = unpack('<II', optional + 128)
    if (resource['flags'] & 0x20000000 or not resource['raw_size']
            or resource_rva != resource['rva'] or resource_size != resource['virtual_size']
            or not 16 <= resource_size <= resource['raw_size']):
        raise ValueError('Invalid or executable resource section')
    leaves = {}
    visited = set()

    def resource_unpack(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > resource_size:
            raise ValueError('Resource structure exceeds its directory')
        return unpack(fmt, resource['offset'] + offset)

    def directory(relative, identity, ancestors):
        if relative in ancestors or relative in visited or len(identity) >= 3:
            raise ValueError('Invalid resource directory depth or cycle')
        visited.add(relative)
        named, numbered = resource_unpack('<HH', relative + 12)
        for index in range(named + numbered):
            name, target = resource_unpack('<II', relative + 16 + index * 8)
            if bool(name & 0x80000000) != (index < named):
                raise ValueError('Invalid named resource ordering')
            if name & 0x80000000:
                position = name & 0x7fffffff
                length, = resource_unpack('<H', position)
                resource_unpack(f'<{length * 2}s', position + 2)
                start = resource['offset'] + position + 2
                name = data[start:start + length * 2].decode('utf-16-le')
            elif name > 0xffff:
                raise ValueError('Invalid resource identifier')
            key = identity + (name,)
            if target & 0x80000000:
                directory(target & 0x7fffffff, key, ancestors | {relative})
            else:
                if len(key) != 3 or key in leaves:
                    raise ValueError('Invalid or duplicate resource leaf')
                rva, size, codepage, reserved = resource_unpack('<IIII', target)
                offset = rva - resource_rva
                if reserved or offset < 0 or offset + size > resource_size:
                    raise ValueError('Resource payload exceeds its directory')
                start = resource['offset'] + offset
                leaves[key] = dict(size=size, sha256=hashlib.sha256(data[start:start + size]).hexdigest(),
                                   codepage=codepage)

    directory(0, (), set())
    if not leaves:
        raise ValueError('Empty resource tree')
    return dict(sections=sections, resource=resource, leaves=leaves, alignment=alignment,
                fields={'SizeOfInitializedData': optional + 8,
                        'ResourceDirectorySize': optional + 132,
                        'ResourceVirtualSize': resource['position'] + 8})


def compare_resource_update(compiled, staged):
    """Accept only the observed resource reserialization; preserve every other byte."""
    before, after = compiled.read_bytes(), staged.read_bytes()
    if len(before) != len(after):
        raise ValueError('PE file length changed')
    original, updated = _pe(before), _pe(after)
    if original['fields'] != updated['fields'] or original['alignment'] != updated['alignment']:
        raise ValueError('PE header layout changed')
    old_sections, new_sections = original['sections'], updated['sections']
    if len(old_sections) != len(new_sections):
        raise ValueError('PE section count changed')
    for left, right in zip(old_sections, new_sections):
        ignored = {'virtual_size'} if left is original['resource'] else set()
        if {k: v for k, v in left.items() if k not in ignored} != {
                k: v for k, v in right.items() if k not in ignored}:
            raise ValueError('PE section layout changed')
    fields = original['fields']
    values = {name: (struct.unpack_from('<I', before, offset)[0],
                     struct.unpack_from('<I', after, offset)[0]) for name, offset in fields.items()}
    initialized_before, initialized_after = values['SizeOfInitializedData']
    if initialized_before != initialized_after:
        initialized = [s for s in old_sections if s['flags'] & 0x40]
        alignment = original['alignment']
        virtual_total = sum((s['virtual_size'] + alignment - 1) // alignment * alignment for s in initialized)
        raw_total = sum(s['raw_size'] for s in initialized)
        if (initialized_before, initialized_after) != (virtual_total, raw_total):
            raise ValueError('Unexplained SizeOfInitializedData change')
    if original['leaves'].keys() != updated['leaves'].keys():
        raise ValueError('Resource identities changed')
    leaf_evidence = []
    for identity in sorted(original['leaves'], key=repr):
        left, right = original['leaves'][identity], updated['leaves'][identity]
        if (left['size'] != right['size'] or left['sha256'] != right['sha256']
                or (left['codepage'] != right['codepage'] and (left['codepage'], right['codepage']) != (0, 1252))):
            raise ValueError('Resource payload or unsupported codepage changed')
        leaf_evidence.append(dict(identity=list(identity), size=left['size'], sha256=left['sha256'],
                                  compiled_codepage=left['codepage'], staged_codepage=right['codepage']))
    resource = original['resource']
    excluded = [(offset, 4) for offset in fields.values()] + [(resource['offset'], resource['raw_size'])]

    def normalized_hash(data):
        digest = hashlib.sha256()
        cursor = 0
        for offset, size in sorted(excluded):
            digest.update(data[cursor:offset])
            for start in range(0, size, 65536):
                digest.update(b'\0' * min(65536, size - start))
            cursor = offset + size
        digest.update(data[cursor:])
        return digest.hexdigest()

    normalized = normalized_hash(before)
    if normalized != normalized_hash(after):
        raise ValueError('Non-resource PE bytes changed')
    offset, size = resource['offset'], resource['raw_size']
    return dict(comparison='ue418_resource_update', compiled_path=str(compiled), staged_path=str(staged),
                file_size=len(before),
                compiled_sha256=hashlib.sha256(before).hexdigest(), staged_sha256=hashlib.sha256(after).hexdigest(),
                normalized_sha256=normalized,
                resource=dict(offset=offset, raw_size=size, rva=resource['rva'], characteristics=resource['flags'],
                              compiled_sha256=hashlib.sha256(before[offset:offset + size]).hexdigest(),
                              staged_sha256=hashlib.sha256(after[offset:offset + size]).hexdigest(), leaves=leaf_evidence),
                header_fields={name: dict(offset=fields[name], compiled=old, staged=new)
                               for name, (old, new) in values.items()})
