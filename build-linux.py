#!/usr/bin/env python3
"""Cross-compile NOLF Modernizer ("Final Release") on Linux with clang-cl + lld-link.

Needs: clang, lld, llvm-rc, ninja, and an MSVC CRT + Windows SDK (x86) splatted by xwin:
    xwin --accept-license --arch x86 splat --output ~/.cache/xwin/splat

Usage: ./build-linux.py [--xwin DIR] [--dist] [ninja args...]
Outputs land in build/; --dist also assembles build/dist like the Azure pipeline (needs wine for lithrez.exe).
"""
import json, os, re, shutil, subprocess, sys, xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, 'build')
NS = {'m': 'http://schemas.microsoft.com/developer/msbuild/2003'}

# (name, vcxproj, configuration, kind, output file)
PROJECTS = [
    ('ButeMgr',   'LT2/lithshared/butemgr/ButeMgr.vcxproj',     'Release',       'lib', 'ButeMgr.lib'),
    ('CryptMgr',  'LT2/lithshared/cryptmgr/cryptmgr.vcxproj',   'Release',       'lib', 'cryptmgr.lib'),
    ('MFCStub',   'LT2/lithshared/mfcstub/MFCStub60.vcxproj',   'Release',       'lib', 'MFCStub.lib'),
    ('ClientRes', 'NOLF/ClientRes/ClientRes.vcxproj',           'Release',       'dll', 'CRes.dll'),
    ('CShell',    'NOLF/ClientShellDLL/ClientShellDLL.vcxproj', 'Final Release', 'dll', 'CShell.dll'),
    ('Object',    'NOLF/ObjectDLL/Object.vcxproj',              'Final Release', 'dll', 'Object.lto'),
]
SOLUTION_DIR = os.path.join(ROOT, 'NOLF') + '/'
# what %(AdditionalDependencies) expands to in MSBuild
MSBUILD_DEFAULT_LIBS = ['kernel32.lib', 'user32.lib', 'gdi32.lib', 'winspool.lib', 'comdlg32.lib', 'advapi32.lib',
                        'shell32.lib', 'ole32.lib', 'oleaut32.lib', 'uuid.lib']


def cond_matches(el, config):
    c = el.get('Condition')
    return c is None or f"=='{config}|Win32'" in c


def prop(group, tag, config):
    for el in group.iter(f'{{{NS["m"]}}}{tag}') if group is not None else ():
        if cond_matches(el, config) and el.text:
            return el.text
    return ''


def split_list(value, base):
    out = []
    for item in value.replace('$(SolutionDir)', SOLUTION_DIR).split(';'):
        item = item.strip()
        if item and not item.startswith('%('):
            out.append(item.replace('\\', '/'))
    return out


def ci(path):
    """Resolve an absolute path whose components may be cased differently than on disk."""
    out = '/'
    for part in path.strip('/').split('/'):
        cand = os.path.join(out, part)
        if not os.path.exists(cand) and os.path.isdir(out):
            cand = os.path.join(out, next((e for e in os.listdir(out) if e.lower() == part.lower()), part))
        out = cand
    return out


def parse(vcxproj, config):
    tree = ET.parse(os.path.join(ROOT, vcxproj))
    base = os.path.dirname(os.path.join(ROOT, vcxproj))
    idg = next((g for g in tree.getroot().findall('m:ItemDefinitionGroup', NS) if cond_matches(g, config)), None)
    cl = idg.find('m:ClCompile', NS) if idg is not None else None
    link = idg.find('m:Link', NS) if idg is not None else None
    absp = lambda p: ci(os.path.normpath(os.path.join(base, p)))
    return {
        'base': base,
        'srcs': [absp(e.get('Include').replace('\\', '/')) for e in tree.getroot().iter(f'{{{NS["m"]}}}ClCompile') if e.get('Include')],
        'rcs': [absp(e.get('Include').replace('\\', '/')) for e in tree.getroot().iter(f'{{{NS["m"]}}}ResourceCompile') if e.get('Include')],
        'incs': [absp(p) for p in split_list(prop(cl, 'AdditionalIncludeDirectories', config), base)],
        'defs': split_list(prop(cl, 'PreprocessorDefinitions', config), base),
        'libs': split_list(prop(link, 'AdditionalDependencies', config), base),
        'nodefault': [l for l in prop(link, 'IgnoreSpecificDefaultLibraries', config).split(';') if l and not l.startswith('%(')],
        'libdirs': [absp(p) for p in split_list(prop(link, 'AdditionalLibraryDirectories', config), base)],
        'std': prop(cl, 'LanguageStandard', config),
    }


def vfs_overlay(path):
    """The sources were written for a case-insensitive FS; let clang/lld look up repo files ignoring case."""
    def tree(d):
        out = []
        for e in os.scandir(d):
            if e.name.startswith('.') or e.name == 'build':
                continue
            if e.is_dir(follow_symlinks=False):
                out.append({'type': 'directory', 'name': e.name, 'contents': tree(e.path)})
            else:
                out.append({'type': 'file', 'name': e.name, 'external-contents': e.path})
        return out
    roots = [{'type': 'directory', 'name': os.path.join(ROOT, d), 'contents': tree(os.path.join(ROOT, d))}
             for d in ('NOLF', 'LT2', 'LIBS')]
    with open(path, 'w') as f:
        json.dump({'version': 0, 'case-sensitive': 'false', 'roots': roots}, f)


def main():
    args = sys.argv[1:]
    xwin = os.path.expanduser('~/.cache/xwin/splat')
    if '--xwin' in args:
        i = args.index('--xwin'); xwin = args[i + 1]; del args[i:i + 2]
    dist = '--dist' in args
    args = [a for a in args if a != '--dist']
    if not os.path.isdir(os.path.join(xwin, 'crt')):
        sys.exit(f'xwin splat not found at {xwin} (see docstring)')

    os.makedirs(BUILD, exist_ok=True)
    overlay = os.path.join(BUILD, 'vfsoverlay.json')
    vfs_overlay(overlay)
    # MFC isn't in xwin; the .rc files only need afxres.h for the winres.h basics
    shim = os.path.join(BUILD, 'shim')
    os.makedirs(shim, exist_ok=True)
    with open(os.path.join(shim, 'afxres.h'), 'w') as f:
        f.write('#include <winres.h>\n')

    sys_incs = [f'{xwin}/crt/include'] + [f'{xwin}/sdk/include/{d}' for d in ('ucrt', 'um', 'shared')]
    sys_libdirs = [f'{xwin}/crt/lib/x86', f'{xwin}/sdk/lib/um/x86', f'{xwin}/sdk/lib/ucrt/x86']
    cflags = ['--target=i686-pc-windows-msvc', '-fms-compatibility-version=19.29', '/nologo', '/MT', '/O2', '/GF', '/Gy',
              '/EHsc', '/Zc:forScope-', '-w', '-Wno-invalid-token-paste', '-Wno-address-of-temporary', '-ferror-limit=0', '-vfsoverlay', overlay]
    cflags += [f'-imsvc{d}' for d in sys_incs]

    n = ['ninja_required_version = 1.5',
         f'cflags = {" ".join(cflags)}',
         'rule cc\n  command = clang-cl $cflags $pflags /showIncludes:user /c $in /Fo$out\n  deps = msvc\n  description = CC $in',
         'rule rc\n  command = llvm-rc /nologo $rcflags /FO $out $in\n  description = RC $in',
         'rule lib\n  command = llvm-lib /nologo /out:$out $in\n  description = LIB $out',
         'rule link\n  command = lld-link /nologo /dll /machine:x86 /subsystem:windows /safeseh:no /opt:ref /opt:icf '
         f'/vfsoverlay:{overlay} ' + ' '.join(f'/libpath:{d}' for d in sys_libdirs) + ' $lflags /out:$out $in\n  description = LINK $out']

    built_libs = {}
    outputs = []
    for name, vcxproj, config, kind, outname in PROJECTS:
        p = parse(vcxproj, config)
        pflags = [f'/D{d}' for d in p['defs']] + [f'/I{i}' for i in p['incs']] + [f'/I{p["base"]}']
        if p['std']:
            pflags.append('/std:' + p['std'].replace('stdcpp', 'c++'))
        objs = []
        for src in p['srcs']:
            obj = os.path.join(BUILD, name, os.path.splitext(os.path.relpath(src, ROOT))[0].replace('/', '_') + '.obj')
            n.append(f'build {obj}: cc {src}\n  pflags = {" ".join(pflags)}')
            objs.append(obj)
        for rc in p['rcs']:
            res = os.path.join(BUILD, name, os.path.basename(rc) + '.res')
            rcflags = ' '.join([f'/I{i}' for i in p['incs'] + [p['base'], shim] + sys_incs] + ['/DNDEBUG', '/DWIN32', '/L', '0x409', '/C', '1252'])
            n.append(f'build {res}: rc {rc}\n  rcflags = {rcflags}')
            objs.append(res)
        out = os.path.join(BUILD, outname)
        if kind == 'lib':
            n.append(f'build {out}: lib {" ".join(objs)}')
            built_libs[outname.lower()] = out
        else:
            libs, deps = [], []
            for lib in p['libs']:
                if lib.lower() in built_libs:
                    deps.append(built_libs[lib.lower()])
                else:
                    libs.append(lib)
            lflags = [f'/libpath:{d}' for d in p['libdirs']] + libs + MSBUILD_DEFAULT_LIBS + [f"'/nodefaultlib:{l}'" for l in p['nodefault']]
            n.append(f'build {out}: link {" ".join(objs + deps)}\n  lflags = {" ".join(lflags)}')
            outputs.append(out)
    n.append(f'default {" ".join(outputs)}')
    with open(os.path.join(BUILD, 'build.ninja'), 'w') as f:
        f.write('\n'.join(n) + '\n')

    subprocess.run(['ninja', '-C', BUILD] + args, check=True)
    if dist:
        make_dist(outputs)


def make_dist(outputs):
    d = os.path.join(BUILD, 'dist')
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, 'BIN'), d)
    shutil.copy(os.path.join(ROOT, 'LIBS/SDL2-2.0.10/lib/x86/SDL2.dll'), d)
    stage = os.path.join(BUILD, 'rezstage')
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, 'ASSETS'), stage)
    for o in outputs:
        shutil.copy(o, stage)
    os.makedirs(os.path.join(d, 'Custom'))
    subprocess.run(['wine', os.path.join(ROOT, 'TOOLS/lithrez.exe'), 'c', os.path.join(d, 'Custom/Modernizer.rez'), stage], check=True)
    print('dist ready:', d)


if __name__ == '__main__':
    main()
