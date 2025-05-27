#!/usr/bin/env python3

import argparse
import base64
import dataclasses
import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union

import requests
from looseversion import LooseVersion

# --- Constants ---
BUILDER_USER = "builder"
BUILDER_HOME = Path(os.getenv("BUILDER_HOME_OVERRIDE", f"/home/{BUILDER_USER}"))
GITHUB_WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", "/github/workspace"))
NVCHECKER_RUN_TEMP_DIR_BASE = BUILDER_HOME / "nvchecker_run"
PACKAGE_BUILD_TEMP_DIR_BASE = BUILDER_HOME / "pkg_builds"
ARTIFACTS_OUTPUT_DIR_BASE = GITHUB_WORKSPACE / "artifacts"
COMMIT_MESSAGE_FILENAME = os.getenv("COMMIT_MESSAGE_FILE", "WORKFLOW_COMMIT_MESSAGE.txt")


# --- GitHub Actions Logging Helpers ---
def _log_gha(level: str, title: str, message: str, file: Optional[str] = None, line: Optional[str] = None, end_line: Optional[str] = None, col: Optional[str] = None, end_column: Optional[str] = None):
    sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    sanitized_title = str(title).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    props = [f"title={sanitized_title}"]
    if file: props.append(f"file={file}")
    if line: props.append(f"line={line}")
    if end_line: props.append(f"endLine={end_line}")
    if col: props.append(f"col={col}")
    if end_column: props.append(f"endColumn={end_column}")
    print(f"::{level} {','.join(props)}::{sanitized_message}")

def log_notice(title: str, message: str, **kwargs): _log_gha("notice", title, message, **kwargs)
def log_error(title: str, message: str, **kwargs): _log_gha("error", title, message, **kwargs)
def log_warning(title: str, message: str, **kwargs): _log_gha("warning", title, message, **kwargs)
def log_debug(message: str, **kwargs):
    if os.getenv("ACTIONS_STEP_DEBUG", "false").lower() == "true":
        sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f"::debug::{sanitized_message}")
def start_group(title: str): print(f"::group::{title}")
def end_group(): print(f"::endgroup::")


# --- Configuration Dataclass ---
@dataclasses.dataclass
class Config:
    aur_maintainer_name: str
    github_repo: str
    pkgbuild_files_root_in_workspace: Path
    git_commit_user_name: str
    git_commit_user_email: str
    gh_token: str
    nvchecker_run_dir: Path = NVCHECKER_RUN_TEMP_DIR_BASE
    package_build_base_dir: Path = PACKAGE_BUILD_TEMP_DIR_BASE
    artifacts_dir_base: Path = ARTIFACTS_OUTPUT_DIR_BASE
    package_status_report_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "package_status_report.json"
    package_build_inputs_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "package_build_inputs.json"
    nvchecker_keyfile_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "nv_keyfile.toml"
    debug_mode: bool = os.getenv("ACTIONS_STEP_DEBUG", "false").lower() == "true"
    github_run_id: Optional[str] = os.getenv("GITHUB_RUN_ID_FOR_ARTIFACTS")
    github_step_summary_file: Optional[Path] = Path(os.getenv("GITHUB_STEP_SUMMARY_FILE_PATH")) if os.getenv("GITHUB_STEP_SUMMARY_FILE_PATH") else None
    updated_pkgbases_in_workspace: set = dataclasses.field(default_factory=set) # Stores pkgbases whose files were updated in workspace

# --- Data Structures ---
@dataclasses.dataclass
class PKGBUILDInfo:
    pkgbase: Optional[str] = None; pkgname: Optional[str] = None; all_pkgnames: List[str] = dataclasses.field(default_factory=list)
    pkgver: Optional[str] = None; pkgrel: Optional[str] = None
    depends: List[str] = dataclasses.field(default_factory=list); makedepends: List[str] = dataclasses.field(default_factory=list)
    checkdepends: List[str] = dataclasses.field(default_factory=list); sources: List[str] = dataclasses.field(default_factory=list)
    pkgfile_abs_path: Optional[Path] = None; error: Optional[str] = None

@dataclasses.dataclass
class PackageOverallStatus:
    pkgbase: str; pkgname_display: str
    local_pkgbuild_info: Optional[PKGBUILDInfo] = None
    aur_version_str: Optional[str] = None; aur_pkgver: Optional[str] = None; aur_pkgrel: Optional[str] = None; aur_actual_pkgname: Optional[str] = None
    nvchecker_new_version: Optional[str] = None; nvchecker_event: Optional[str] = None; nvchecker_raw_log: Optional[Dict[str, Any]] = None
    comparison_errors: List[str] = dataclasses.field(default_factory=list); is_update_candidate: bool = True; needs_update: bool = False
    update_source_type: Optional[str] = None; version_for_update: Optional[str] = None; local_is_ahead: bool = False
    comparison_log: Dict[str, str] = dataclasses.field(default_factory=dict)
    pkgbuild_dir_rel_to_workspace: Optional[Path] = None

@dataclasses.dataclass
class BuildOpResult:
    package_name: str; success: bool = False
    target_version_for_build: Optional[str] = None; final_pkgbuild_version_in_clone: Optional[str] = None
    built_package_archive_files: List[Path] = dataclasses.field(default_factory=list)
    setup_env_ok: bool = False; dependencies_installed_ok: bool = False; pkgbuild_versioned_ok: bool = False
    makepkg_ran_ok: bool = False; local_install_ok: bool = False
    changes_made_to_aur_clone_files: bool = False # Tracks if files in AUR clone were changed (PKGBUILD, .SRCINFO etc.)
    git_commit_to_aur_ok: bool = False; git_push_to_aur_ok: bool = False; github_release_ok: bool = False
    workspace_files_updated_ok: bool = False # Tracks if files were copied back to workspace
    error_message: Optional[str] = None; log_artifact_subdir: Optional[Path] = None
    package_specific_build_dir_abs: Optional[Path] = None; aur_clone_dir_abs: Optional[Path] = None

# --- Command Runner ---
class CommandRunner: # (Content unchanged, kept for brevity)
    def __init__(self, logger: logging.Logger, default_user: Optional[str] = None, default_home: Optional[Path] = None):
        self.logger = logger
        self.default_user = default_user
        self.default_home = default_home

    def run(self, cmd_list: List[str], check: bool = True, cwd: Optional[Path] = None,
            env_extra: Optional[Dict[str, str]] = None, capture_output: bool = True,
            print_command: bool = True, input_data: Optional[str] = None,
            run_as_user: Optional[str] = None, user_home_dir: Optional[Path] = None
            ) -> subprocess.CompletedProcess:
        user_to_run = run_as_user if run_as_user is not None else self.default_user
        home_for_run = user_home_dir if user_home_dir is not None else self.default_home
        final_cmd = list(cmd_list); current_env = os.environ.copy()
        if user_to_run:
            sudo_prefix = ["sudo", "-E", "-u", user_to_run]
            if home_for_run: sudo_prefix.append(f"HOME={str(home_for_run)}")
            final_cmd = sudo_prefix + final_cmd
            if user_to_run == BUILDER_USER: current_env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{home_for_run / '.local/bin' if home_for_run else ''}"
        if env_extra: current_env.update(env_extra)
        if print_command: log_debug(f"Running command: {shlex.join(final_cmd)} (CWD: {cwd or '.'})")
        try:
            result = subprocess.run(final_cmd, check=check, text=True, capture_output=capture_output, cwd=cwd, env=current_env, input=input_data, timeout=1800)
            if result.stdout and print_command and capture_output: log_debug(f"CMD STDOUT: {result.stdout.strip()[:500]}")
            if result.stderr and print_command and capture_output: log_debug(f"CMD STDERR: {result.stderr.strip()[:500]}")
            return result
        except subprocess.CalledProcessError as e:
            log_error("CMD_FAIL", f"Command '{shlex.join(e.cmd)}' failed with RC {e.returncode}. STDERR: {e.stderr.strip()[:500] if e.stderr else 'N/A'}")
            if e.stdout: log_debug(f"FAIL STDOUT: {e.stdout.strip()}")
            if check: raise
            return e
        except subprocess.TimeoutExpired as e:
            log_error("CMD_TIMEOUT", f"Command '{shlex.join(final_cmd)}' timed out.");
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=124, stdout=e.stdout or "", stderr=e.stderr or "TimeoutExpired")
        except FileNotFoundError as e:
            log_error("CMD_NOT_FOUND", f"Command not found: {final_cmd[0]}.");
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=127, stdout="", stderr=str(e))
        except Exception as e:
            log_error("CMD_UNEXPECTED_ERROR", f"Unexpected error running '{shlex.join(final_cmd)}': {type(e).__name__} - {e}")
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=1, stdout="", stderr=str(e))

# --- PKGBUILD Parser ---
class PKGBUILDParser: # (Content mostly unchanged, minor logging tweaks if any, kept for brevity)
    def __init__(self, runner: CommandRunner, logger: logging.Logger, config: Config):
        self.runner = runner; self.logger = logger; self.config = config
    def _source_and_extract_pkgbuild_vars(self, pkgbuild_file_path: Path) -> PKGBUILDInfo:
        info = PKGBUILDInfo(pkgfile_abs_path=pkgbuild_file_path); escaped_pkgbuild_path = shlex.quote(str(pkgbuild_file_path))
        bash_script = f"""
        _SHELL_OPTS_OLD=$(set +o); set -e; PKGBUILD_DIR=$(dirname {escaped_pkgbuild_path}); cd "$PKGBUILD_DIR"
        unset pkgbase pkgname pkgver pkgrel epoch depends makedepends checkdepends optdepends provides conflicts replaces options source md5sums sha1sums sha224sums sha256sums sha384sums sha512sums b2sums
        pkgver() {{ :; }}; prepare() {{ :; }}; build() {{ :; }}; check() {{ :; }}; package() {{ :; }};
        package_prepend() {{ :; }}; package_append() {{ :; }}; for _f in $(declare -F|awk '{{print $3}}'|grep -E '^package_');do eval "$_f(){{ :; }}";done
        set +u; . {escaped_pkgbuild_path}; set -u; set +e
        echo "PKGBASE_START";echo "${{pkgbase:-__VAR_NOT_SET__}}";echo "PKGBASE_END"
        _p="__VAR_NOT_SET__";if [ -n "${{pkgname+x}}" ];then if declare -p pkgname 2>/dev/null|grep -q '^declare -a';then _p="${{pkgname[0]:-__VAR_NOT_SET__}}";else _p="${{pkgname:-__VAR_NOT_SET__}}";fi;fi;echo "PRIMARY_PKGNAME_START";echo "${{_p}}";echo "PRIMARY_PKGNAME_END"
        _pa(){{ v="$1";if [ -n "${{!v+x}}" ]&&declare -p "$v" 2>/dev/null|grep -q '^declare -a';then local -n a="$v";if [ "${{#a[@]}}" -gt 0 ];then printf '%s\\n' "${{a[@]}}";else echo "__EMPTY_ARRAY__";fi;elif [ -n "${{!v+x}}" ];then echo "${{!v}}";else echo "__EMPTY_ARRAY__";fi;}};
        echo "ALL_PKGNAMES_START";_pa pkgname;echo "ALL_PKGNAMES_END"; echo "PKGVER_START";echo "${{pkgver:-__VAR_NOT_SET__}}";echo "PKGVER_END"; echo "PKGREL_START";echo "${{pkgrel:-__VAR_NOT_SET__}}";echo "PKGREL_END"
        echo "DEPENDS_START";_pa depends;echo "DEPENDS_END"; echo "MAKEDEPENDS_START";_pa makedepends;echo "MAKEDEPENDS_END"; echo "CHECKDEPENDS_START";_pa checkdepends;echo "CHECKDEPENDS_END"; echo "SOURCES_START";_pa source;echo "SOURCES_END"
        eval "$_SHELL_OPTS_OLD"
        """
        try:
            result = self.runner.run(['bash', '-c', bash_script], check=False, run_as_user=BUILDER_USER, user_home_dir=BUILDER_HOME)
            if result.stderr.strip(): self.logger.debug(f"Bash stderr for {pkgbuild_file_path} (RC {result.returncode}):\n{result.stderr.strip()}")
            if self.config.debug_mode or result.returncode != 0:
                 if result.stdout.strip(): self.logger.debug(f"Bash stdout for {pkgbuild_file_path}:\n{result.stdout.strip()}")
                 else: self.logger.debug(f"Bash stdout for {pkgbuild_file_path} empty.")
            if result.returncode != 0 and not result.stdout.strip():
                info.error = f"Bash script sourcing failed (RC {result.returncode}) no output."; return info
            output_lines = result.stdout.splitlines()
            def _prs(s, e, a): r=[];iS=False;for lC in output_lines:if lC==s:iS=True;continue;if lC==e:iS=False;break;if iS:r.append(lC); return ([v for v in r if v.strip()] if not r or r==["__EMPTY_ARRAY__"] else r) if a else (None if not r or r[0]=="__VAR_NOT_SET__" else r[0].strip())
            raw_pkgbase_val=_prs("PKGBASE_START","PKGBASE_END",False);raw_primary_pkgname_val=_prs("PRIMARY_PKGNAME_START","PRIMARY_PKGNAME_END",False)
            info.all_pkgnames=_prs("ALL_PKGNAMES_START","ALL_PKGNAMES_END",True)
            if raw_pkgbase_val:info.pkgbase=raw_pkgbase_val
            elif raw_primary_pkgname_val:info.pkgbase=raw_primary_pkgname_val;self.logger.debug(f"Derived pkgbase '{info.pkgbase}' from primary for {pkgbuild_file_path}")
            info.pkgname=raw_primary_pkgname_val if raw_primary_pkgname_val else info.pkgbase
            info.pkgver=_prs("PKGVER_START","PKGVER_END",False);info.pkgrel=_prs("PKGREL_START","PKGREL_END",False)
            if info.pkgrel is None and info.pkgver is not None: info.pkgrel="1"
            info.depends=_prs("DEPENDS_START","DEPENDS_END",True);info.makedepends=_prs("MAKEDEPENDS_START","MAKEDEPENDS_END",True)
            info.checkdepends=_prs("CHECKDEPENDS_START","CHECKDEPENDS_END",True);info.sources=_prs("SOURCES_START","SOURCES_END",True)
            cE=[];
            if result.returncode!=0:eM=f"Sourcing script RC {result.returncode}.";if result.stderr.strip():eM+=f" Stderr: {result.stderr.strip()[:100]}";cE.append(eM)
            if not info.pkgbase and not info.pkgname:cE.append("Neither pkgbase nor primary pkgname determined.")
            if not info.pkgver:cE.append("pkgver not extracted.")
            if cE:info.error="; ".join(cE);self.logger.debug(f"Issues parsing {pkgbuild_file_path}: {info.error}")
        except Exception as e:info.error=f"Python exception PKGBUILD {pkgbuild_file_path}: {type(e).__name__}-{e}";self.logger.error(info.error,exc_info=self.config.debug_mode)
        return info
    def fetch_all_local_pkgbuild_data(self, pkgbuild_root_dir: Path) -> Dict[str, PKGBUILDInfo]:
        start_group("Parsing Local PKGBUILD Files"); self.logger.info(f"Searching PKGBUILDs in {pkgbuild_root_dir}...")
        pkgbuild_files = list(pkgbuild_root_dir.rglob("PKGBUILD")); self.logger.info(f"Found {len(pkgbuild_files)} PKGBUILDs.")
        results_by_pkgbase: Dict[str, PKGBUILDInfo] = {};
        if not pkgbuild_files: self.logger.warning("No PKGBUILD files found."); end_group(); return results_by_pkgbase
        num_workers = min(os.cpu_count() or 1, len(pkgbuild_files)); self.logger.info(f"Processing PKGBUILDs using up to {num_workers} worker(s).")
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            f_to_p = {executor.submit(self._source_and_extract_pkgbuild_vars, fp): fp for fp in pkgbuild_files}
            for f in as_completed(f_to_p):
                o_fp=f_to_p[f];
                try: pI=f.result()
                except Exception as exc: self.logger.error(f"Worker for {o_fp} unhandled exception: {exc}",exc_info=self.config.debug_mode);pI=PKGBUILDInfo(pkgfile_abs_path=o_fp,error=f"Worker exc: {exc}")
                if not pI.pkgbase and pI.pkgver and pI.pkgfile_abs_path:
                    d_f_d=pI.pkgfile_abs_path.parent.name;self.logger.warning(f"PKGBUILD {pI.pkgfile_abs_path.name} no pkgbase/primary_pkgname. Fallback to dir '{d_f_d}' (pkgver: {pI.pkgver}).");pI.pkgbase=d_f_d
                    if not pI.pkgname:pI.pkgname=d_f_d
                    if pI.error and "Neither pkgbase nor a primary pkgname could be determined" in pI.error:pI.error=pI.error.replace("Neither pkgbase nor a primary pkgname could be determined from PKGBUILD variables.","").strip("; ");
                    if not (pI.error or "").strip():pI.error=None
                if not pI.pkgbase: pI.error=(pI.error+"; " if pI.error else "")+"Critical: pkgbase undetermined."
                if not pI.pkgver: pI.error=(pI.error+"; " if pI.error else "")+"Critical: pkgver not extracted."
                if not pI.pkgbase or not pI.pkgver: self.logger.error(f"Skipping {pI.pkgfile_abs_path or o_fp} critical missing data: {pI.error or 'Unknown'}"); continue
                if pI.error: self.logger.warning(f"PKGBUILD {pI.pkgfile_abs_path.name} (pkgbase: {pI.pkgbase}) processed with issues: {pI.error}")
                if pI.pkgbase in results_by_pkgbase: self.logger.warning(f"Duplicate pkgbase '{pI.pkgbase}'. Original: '{results_by_pkgbase[pI.pkgbase].pkgfile_abs_path}'. New: '{pI.pkgfile_abs_path}'. Overwriting.")
                results_by_pkgbase[pI.pkgbase]=pI; self.logger.debug(f"Stored local PKGBUILD: {pI.pkgbase} (Name: {pI.pkgname}, Ver: {pI.pkgver}-{pI.pkgrel}) from: {pI.pkgfile_abs_path.name}")
        self.logger.info(f"Successfully processed {len(results_by_pkgbase)} unique pkgbase entries."); end_group(); return results_by_pkgbase

# --- AUR Info Fetcher ---
class AURInfoFetcher: # (Content unchanged)
    def __init__(self, logger: logging.Logger): self.logger = logger
    def fetch_data_for_maintainer(self, maintainer: str) -> Dict[str, Dict[str, str]]:
        start_group(f"Fetching AUR Data for Maintainer: {maintainer}"); aur_data_by_pkgbase: Dict[str, Dict[str, str]] = {}; url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by=maintainer"; self.logger.info(f"Querying AUR: {url}")
        try:
            response = requests.get(url, timeout=30); response.raise_for_status(); data = response.json()
            if data.get("type") == "error": self.logger.error(f"AUR API error: {data.get('error')}"); end_group(); return aur_data_by_pkgbase
            if data.get("resultcount", 0) == 0: self.logger.info(f"No packages on AUR for maintainer '{maintainer}'."); end_group(); return aur_data_by_pkgbase
            for res in data.get("results", []):
                pb, name, fver = res.get("PackageBase"), res.get("Name"), res.get("Version")
                if not pb or not fver: continue
                v_no_ep = fver.split(':', 1)[-1]; parts = v_no_ep.rsplit('-', 1); b_v, r_v = parts[0], (parts[1] if len(parts) > 1 and parts[1].isdigit() else "0")
                if pb not in aur_data_by_pkgbase: aur_data_by_pkgbase[pb] = {"aur_pkgver":b_v,"aur_pkgrel":r_v,"aur_actual_pkgname":name,"aur_version_str":f"{b_v}-{r_v}" if r_v!="0" else b_v}; self.logger.debug(f"  AUR: {pb} (Name: {name}) -> v{b_v}-{r_v}")
            self.logger.info(f"Fetched info for {len(aur_data_by_pkgbase)} PkgBase(s) from AUR.")
        except requests.Timeout: self.logger.error("AUR query timed out.")
        except requests.RequestException as e: self.logger.error(f"AUR query failed: {e}")
        except json.JSONDecodeError as e: self.logger.error(f"Failed to parse AUR JSON: {e}")
        end_group(); return aur_data_by_pkgbase

# --- NVChecker Runner ---
class NVCheckerRunner: # (Content mostly unchanged)
    def __init__(self, runner: CommandRunner, logger: logging.Logger, config: Config): self.runner, self.logger, self.config = runner, logger, config
    def _generate_nvchecker_keyfile(self) -> Optional[Path]:
        if not self.config.gh_token: log_debug("No GH_TOKEN, nvchecker runs without GitHub keys."); return None
        kc = f"[keys]\ngithub = '{self.config.gh_token}'\n"; self.config.nvchecker_keyfile_path.parent.mkdir(parents=True,exist_ok=True)
        try: self.config.nvchecker_keyfile_path.write_text(kc); self.runner.run(["chown",f"{BUILDER_USER}:{BUILDER_USER}",str(self.config.nvchecker_keyfile_path)],run_as_user="root",check=False); self.logger.info(f"NVChecker keyfile created: {self.config.nvchecker_keyfile_path}"); return self.config.nvchecker_keyfile_path
        except Exception as e: self.logger.error(f"Failed to create NVChecker keyfile: {e}"); return None
    def run_global_nvchecker(self, pkgbuild_root_dir: Path, oldver_data_for_nvchecker: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        start_group("Running Global NVChecker"); results_by_pkgbase: Dict[str, Dict[str, Any]] = {}
        toml_files = list(pkgbuild_root_dir.rglob(".nvchecker.toml"));
        if not toml_files: self.logger.info("No .nvchecker.toml files. Skipping."); end_group(); return results_by_pkgbase
        self.logger.info(f"Found {len(toml_files)} .nvchecker.toml files.")
        all_nv_tomls_path,oldver_json_path,newver_json_path = self.config.nvchecker_run_dir/"all_project_nv.toml",self.config.nvchecker_run_dir/"oldver.json",self.config.nvchecker_run_dir/"newver.json"
        if not self.config.nvchecker_run_dir.is_dir(): self.runner.run(["mkdir","-p",str(self.config.nvchecker_run_dir)],check=True)
        self.runner.run(["chown","-R",f"{BUILDER_USER}:{BUILDER_USER}",str(self.config.nvchecker_run_dir)],check=True,run_as_user="root"); self.logger.info(f"Ensured {self.config.nvchecker_run_dir} exists and builder-owned.")
        keyfile_to_use = self._generate_nvchecker_keyfile()
        try:
            toml_c_list = ["[__config__]\n",f"oldver = '{oldver_json_path.name}'\n",f"newver = '{newver_json_path.name}'\n\n"]
            for tf_path in toml_files:
                try: toml_c_list.extend([f"# Source: {tf_path.relative_to(GITHUB_WORKSPACE)}\n",tf_path.read_text(),"\n\n"])
                except Exception as e: self.logger.warning(f"Error reading {tf_path}: {e}. Skipping."); continue
            self.runner.run(["bash","-c",f"echo {shlex.quote(''.join(toml_c_list))} > {shlex.quote(str(all_nv_tomls_path))}"],check=True); log_debug(f"NVChecker TOML config written to {all_nv_tomls_path.name}")
            oldver_json_content_str = json.dumps({"version":2,"data":oldver_data_for_nvchecker})
            self.runner.run(["bash","-c",f"echo {shlex.quote(oldver_json_content_str)} > {shlex.quote(str(oldver_json_path))}"],check=True); log_debug(f"Oldver JSON written to {oldver_json_path.name}")
            self.runner.run(["touch",str(newver_json_path)],check=True); self.runner.run(["truncate","-s","0",str(newver_json_path)],check=True); log_debug(f"Ensured {newver_json_path.name} is empty.")
            cmd = ['nvchecker','-c',all_nv_tomls_path.name,'--logger=json'];
            if keyfile_to_use and keyfile_to_use.is_file(): cmd.extend(['-k',str(keyfile_to_use)])
            self.logger.info(f"Running NVChecker: {shlex.join(cmd)} (CWD: {self.config.nvchecker_run_dir})")
            proc = self.runner.run(cmd,cwd=self.config.nvchecker_run_dir,check=False,run_as_user=BUILDER_USER,user_home_dir=BUILDER_HOME)
            log_debug(f"NVChecker STDOUT:\n{proc.stdout}");log_debug(f"NVChecker STDERR:\n{proc.stderr}");log_debug(f"NVChecker RC: {proc.returncode}")
            if proc.returncode!=0: self.logger.error(f"NVChecker exited RC {proc.returncode}.")
            if proc.stderr.strip(): self.logger.info(f"NVChecker STDERR (Info/Warnings):\n{proc.stderr.strip()}")
            if not proc.stdout.strip(): self.logger.warning("NVChecker no STDOUT.")
            for ln,line in enumerate(proc.stdout.strip().split('\n')):
                if not line.strip(): continue; log_debug(f"NVCR STDOUT Log Line {ln+1}: {line}")
                try:
                    entry = json.loads(line);
                    if entry.get("logger_name")!="nvchecker.core": log_debug(f"  Skipping log: logger_name '{entry.get('logger_name')}'"); continue
                    pkg_n_nv = entry.get("name");
                    if not pkg_n_nv: log_debug(f"  Skipping log: 'name' missing."); continue
                    cur_res_pkg = results_by_pkgbase.setdefault(pkg_n_nv,{}); cur_res_pkg["nvchecker_raw_log"]=entry
                    event = entry.get("event");
                    if event: cur_res_pkg["nvchecker_event"]=event; self.logger.debug(f"  PKG '{pkg_n_nv}': Event '{event}'")
                    ver_log = entry.get("version")
                    if event=="updated" and ver_log is not None: n_v_s=str(ver_log);cur_res_pkg["nvchecker_new_version"]=n_v_s;self.logger.info(f"  PKG '{pkg_n_nv}': Event 'updated'. New ver '{n_v_s}'. Old: {entry.get('old_version','N/A')}")
                    elif event=="up-to-date": self.logger.info(f"  PKG '{pkg_n_nv}': Event 'up-to-date' at ver '{ver_log}'.")
                    elif event=="no-result": self.logger.warning(f"  PKG '{pkg_n_nv}': Event 'no-result'. Msg: {entry.get('msg','')}")
                    elif entry.get("level")=="error" or entry.get("exc_info"): self.logger.warning(f"  PKG '{pkg_n_nv}': Logged ERROR. Msg: {entry.get('exc_info',entry.get('msg',''))}")
                    if cur_res_pkg.get("nvchecker_new_version") and cur_res_pkg.get("nvchecker_event")!="updated":
                        if cur_res_pkg.get("nvchecker_event"): self.logger.debug(f"  PKG '{pkg_n_nv}': Ensuring event 'updated' (was '{cur_res_pkg.get('nvchecker_event')}').")
                        cur_res_pkg["nvchecker_event"]="updated"
                except json.JSONDecodeError: self.logger.warning(f"Failed parse NVChecker JSON log line: {line[:100]}...")
        except FileNotFoundError: self.logger.critical("nvchecker command not found.")
        except subprocess.CalledProcessError as e_s: self.logger.error(f"Subprocess error NVChecker setup: {e_s}. Cmd: '{e_s.cmd}'. Stderr: {e_s.stderr}",exc_info=self.config.debug_mode)
        except Exception as e: self.logger.error(f"Global NVChecker failed: {e}",exc_info=self.config.debug_mode)
        pkgs_w_new_ver = sum(1 for pd in results_by_pkgbase.values() if pd.get("nvchecker_new_version"))
        self.logger.info(f"NVChecker global run finished. 'nvchecker_new_version' for {pkgs_w_new_ver} pkgbase(s)."); end_group(); return results_by_pkgbase
    def run_nvchecker_for_package_aur_clone(self, nvchecker_toml_in_aur_clone_path: Path) -> Optional[str]:
        if not nvchecker_toml_in_aur_clone_path.is_file(): self.logger.debug(f"No .nvchecker.toml at {nvchecker_toml_in_aur_clone_path}, skipping single nvcheck."); return None
        start_group(f"NVCheck single: {nvchecker_toml_in_aur_clone_path.parent.name}"); new_ver = None
        try:
            cmd = ['nvchecker','-c',str(nvchecker_toml_in_aur_clone_path)];
            if self.config.nvchecker_keyfile_path.is_file(): cmd.extend(['-k',str(self.config.nvchecker_keyfile_path)])
            proc = self.runner.run(cmd,cwd=nvchecker_toml_in_aur_clone_path.parent,check=False,run_as_user=BUILDER_USER,user_home_dir=BUILDER_HOME)
            upd_pat = re.compile(r":\s*updated to\s+([^\s,]+)",re.IGNORECASE); pkg_n_dir = nvchecker_toml_in_aur_clone_path.parent.name
            for line in proc.stderr.splitlines():
                line = line.strip();
                if not line: continue
                m = upd_pat.search(line);
                if m and (pkg_n_dir in line or "updated to" in line): new_ver=m.group(1);self.logger.info(f"NVChecker (single stderr) new ver: {new_ver} for {pkg_n_dir}");break
                elif f"{pkg_n_dir}: current" in line: self.logger.info(f"NVChecker (single stderr) reports {pkg_n_dir} current."); break
            if not new_ver: self.logger.info(f"NVChecker (single) no new ver for {pkg_n_dir} via stderr.")
        except Exception as e: self.logger.error(f"NVChecker single {nvchecker_toml_in_aur_clone_path.parent.name} failed: {e}",exc_info=self.config.debug_mode)
        end_group(); return new_ver

# --- Version Comparison Logic ---
class VersionComparator: # (Content unchanged)
    def __init__(self, logger: logging.Logger): self.logger = logger
    def _normalize_version_str(self, ver_str: str) -> str: return ver_str.replace('_','.')
    def compare_pkg_versions(self, b1_s: Optional[str], r1_s: Optional[str], b2_s: Optional[str], r2_s: Optional[str]) -> str:
        if b1_s is None and b2_s is None: return "same"
        if b1_s is None: return "upgrade";
        if b2_s is None: return "downgrade"
        try: lv1,lv2=LooseVersion(self._normalize_version_str(b1_s)),LooseVersion(self._normalize_version_str(b2_s))
        except Exception as e: self.logger.error(f"Error LooseVersion '{b1_s}' or '{b2_s}': {e}"); return "unknown"
        if lv1<lv2: return "upgrade";
        if lv1>lv2: return "downgrade"
        try: nr1,nr2=int(r1_s or 0),int(r2_s or 0)
        except ValueError: self.logger.error(f"Invalid non-int release: '{r1_s}' or '{r2_s}' with base '{b1_s}'."); return "unknown"
        if nr1<nr2: return "upgrade";
        if nr1>nr2: return "downgrade"
        return "same"
    def get_full_version_string(self, v_s: Optional[str], r_s: Optional[str]) -> Optional[str]:
        if not v_s: return None; r_v=str(r_s) if r_s is not None and str(r_s).strip() and str(r_s)!="0" else ""; return f"{v_s}-{r_v}" if r_v else v_s

# --- Main Application Logic ---
class ArchPackageManager:
    def __init__(self, config: Config):
        self.config = config; self.logger = logging.getLogger("ArchPkgMgr"); self._configure_logger()
        self.runner = CommandRunner(self.logger); self.builder_runner = CommandRunner(self.logger,BUILDER_USER,BUILDER_HOME)
        self.pkgbuild_parser = PKGBUILDParser(self.builder_runner,self.logger,self.config)
        self.aur_fetcher = AURInfoFetcher(self.logger)
        self.nvchecker = NVCheckerRunner(self.builder_runner,self.logger,self.config)
        self.version_comparator = VersionComparator(self.logger)
        self.build_operation_results: List[BuildOpResult] = []

    def _configure_logger(self):
        log_level=logging.DEBUG if self.config.debug_mode else logging.INFO;self.logger.setLevel(log_level)
        if not self.logger.hasHandlers(): h=logging.StreamHandler(sys.stderr);h.setFormatter(logging.Formatter('%(levelname)s:[%(name)s] %(message)s'));self.logger.addHandler(h)
        self.logger.propagate=False;log_notice("Logger Config",f"Logger '{self.logger.name}' configured. Level: {logging.getLevelName(self.logger.getEffectiveLevel())}")

    def _initial_environment_setup(self) -> bool:
        start_group("Initial Environment Setup"); self.logger.info(f"WS: {GITHUB_WORKSPACE}, PKGBUILDs: {self.config.pkgbuild_files_root_in_workspace}")
        for d_p in [self.config.nvchecker_run_dir,self.config.package_build_base_dir]:
            try: self.runner.run(["mkdir","-p",str(d_p)]);self.runner.run(["chown",f"{BUILDER_USER}:{BUILDER_USER}",str(d_p)]);self.logger.info(f"Ensured dir builder-owned: {d_p}")
            except Exception as e: log_error("EnvSetupFail",f"Failed create/chown dir {d_p}: {e}");end_group();return False
        if not self.config.artifacts_dir_base.is_dir(): log_error("EnvSetupFail",f"Base artifacts dir {self.config.artifacts_dir_base} missing.");end_group();return False
        self.logger.info(f"Base artifacts dir confirmed: {self.config.artifacts_dir_base}")
        if not self.config.pkgbuild_files_root_in_workspace.is_dir(): log_error("Config Error",f"PKGBUILD_FILES_ROOT '{self.config.pkgbuild_files_root_in_workspace}' invalid.");end_group();return False
        log_notice("EnvSetup","Initial env setup complete.");end_group();return True

    def _analyze_package_statuses(self, local_data: Dict[str, PKGBUILDInfo], aur_data: Dict[str, Dict[str, str]], nvchecker_data: Dict[str, Dict[str, Any]]) -> List[PackageOverallStatus]:
        start_group("Analyzing Package Statuses");all_pb=set(local_data.keys())|set(aur_data.keys())|set(nvchecker_data.keys());proc_stat:List[PackageOverallStatus]=[];self.logger.info(f"Found {len(all_pb)} unique pkgbases.")
        for pb in sorted(list(all_pb)):
            loc_i,aur_i,nv_i=local_data.get(pb),aur_data.get(pb),nvchecker_data.get(pb)
            if not loc_i or not loc_i.pkgfile_abs_path: self.logger.debug(f"Skipping {pb}: No local PKGBUILD."); continue
            stat=PackageOverallStatus(pkgbase=pb,pkgname_display=loc_i.pkgname or pb,local_pkgbuild_info=loc_i)
            try: stat.pkgbuild_dir_rel_to_workspace=loc_i.pkgfile_abs_path.parent.relative_to(GITHUB_WORKSPACE)
            except ValueError: stat.comparison_errors.append(f"PKGBUILD path not relative to workspace.");stat.is_update_candidate=False
            if aur_i: stat.aur_pkgver,stat.aur_pkgrel,stat.aur_actual_pkgname,stat.aur_version_str=aur_i.get("aur_pkgver"),aur_i.get("aur_pkgrel"),aur_i.get("aur_actual_pkgname"),aur_i.get("aur_version_str")
            if nv_i: stat.nvchecker_new_version,stat.nvchecker_event,stat.nvchecker_raw_log=nv_i.get("nvchecker_new_version"),nv_i.get("nvchecker_event"),nv_i.get("nvchecker_raw_log")
            if stat.aur_pkgver and stat.nvchecker_new_version:
                comp_an=self.version_comparator.compare_pkg_versions(stat.aur_pkgver,None,stat.nvchecker_new_version,None);stat.comparison_log["aur_vs_nv_base"]=f"AUR Base ({stat.aur_pkgver}) vs NV Base ({stat.nvchecker_new_version}) -> {comp_an}"
                if comp_an=="downgrade": stat.comparison_errors.append(f"AUR base {stat.aur_pkgver} > NV upstream {stat.nvchecker_new_version}.");stat.is_update_candidate=False
            if loc_i.pkgver and stat.nvchecker_new_version:
                comp_ln=self.version_comparator.compare_pkg_versions(loc_i.pkgver,None,stat.nvchecker_new_version,None);stat.comparison_log["local_vs_nv_base"]=f"Local Base ({loc_i.pkgver}) vs NV Base ({stat.nvchecker_new_version}) -> {comp_ln}"
                if comp_ln=="downgrade": stat.comparison_errors.append(f"Local PKGBUILD base {loc_i.pkgver} > NV upstream {stat.nvchecker_new_version}.");stat.is_update_candidate=False
            if not stat.is_update_candidate: proc_stat.append(stat);self.logger.info(f"{pb}: Not update candidate: {stat.comparison_errors}");continue
            upd_nv=False
            if stat.nvchecker_new_version:
                comp_l_nv=self.version_comparator.compare_pkg_versions(loc_i.pkgver,loc_i.pkgrel,stat.nvchecker_new_version,None);stat.comparison_log["local_full_vs_nv_base"]=f"Local Full ({self.version_comparator.get_full_version_string(loc_i.pkgver,loc_i.pkgrel)}) vs NV Base ({stat.nvchecker_new_version}) -> {comp_l_nv}"
                aur_older_nv=True
                if stat.aur_pkgver:
                    comp_a_nv=self.version_comparator.compare_pkg_versions(stat.aur_pkgver,stat.aur_pkgrel,stat.nvchecker_new_version,None);stat.comparison_log["aur_full_vs_nv_base"]=f"AUR Full ({stat.aur_version_str}) vs NV Base ({stat.nvchecker_new_version}) -> {comp_a_nv}"
                    if comp_a_nv!="upgrade": aur_older_nv=False
                if comp_l_nv=="upgrade" and aur_older_nv: stat.needs_update,stat.update_source_type,stat.version_for_update=True,"nvchecker (upstream)",stat.nvchecker_new_version;upd_nv=True;self.logger.info(f"{pb}: Update via NVChecker to {stat.version_for_update}")
            if not upd_nv and stat.aur_pkgver:
                comp_l_aur=self.version_comparator.compare_pkg_versions(loc_i.pkgver,loc_i.pkgrel,stat.aur_pkgver,stat.aur_pkgrel);stat.comparison_log["local_full_vs_aur_full"]=f"Local Full ({self.version_comparator.get_full_version_string(loc_i.pkgver,loc_i.pkgrel)}) vs AUR Full ({stat.aur_version_str}) -> {comp_l_aur}"
                if comp_l_aur=="upgrade": stat.needs_update,stat.update_source_type,stat.version_for_update=True,"aur",stat.aur_version_str;self.logger.info(f"{pb}: Update via AUR to {stat.version_for_update}")
            if not stat.needs_update and not stat.comparison_errors and loc_i.pkgver:
                ah_aur=(self.version_comparator.compare_pkg_versions(loc_i.pkgver,loc_i.pkgrel,stat.aur_pkgver,stat.aur_pkgrel)=="downgrade") if stat.aur_pkgver else True
                ah_nv=(self.version_comparator.compare_pkg_versions(loc_i.pkgver,loc_i.pkgrel,stat.nvchecker_new_version,None)=="downgrade") if stat.nvchecker_new_version else True
                exp_c=(1 if stat.aur_pkgver else 0)+(1 if stat.nvchecker_new_version else 0);act_ah_c=(1 if ah_aur and stat.aur_pkgver else 0)+(1 if ah_nv and stat.nvchecker_new_version else 0)
                if(exp_c>0 and act_ah_c==exp_c)or exp_c==0: stat.local_is_ahead=True
                if stat.local_is_ahead: self.logger.info(f"{pb}: Local ver {self.version_comparator.get_full_version_string(loc_i.pkgver,loc_i.pkgrel)} is ahead.")
            if not stat.needs_update and not stat.local_is_ahead and not stat.comparison_errors: self.logger.info(f"{pb}: Up-to-date or no action.")
            proc_stat.append(stat)
        end_group();return proc_stat

    def _prepare_inputs_for_build_script(self, packages_for_build: List[PackageOverallStatus]) -> bool:
        start_group("Preparing Build Script Inputs");bld_in_data:Dict[str,Dict[str,Any]]={};
        if not packages_for_build: self.logger.info("No packages to build, input JSON empty.")
        for pkg_s in packages_for_build:
            if pkg_s.local_pkgbuild_info: bld_in_data[pkg_s.pkgbase]={"depends":pkg_s.local_pkgbuild_info.depends,"makedepends":pkg_s.local_pkgbuild_info.makedepends,"checkdepends":pkg_s.local_pkgbuild_info.checkdepends,"sources":pkg_s.local_pkgbuild_info.sources}
        try:
            self.config.package_build_inputs_path.parent.mkdir(parents=True,exist_ok=True); self.config.package_build_inputs_path.write_text(json.dumps(bld_in_data,indent=2))
            self.runner.run(["chown",f"{BUILDER_USER}:{BUILDER_USER}",str(self.config.package_build_inputs_path)],run_as_user="root",check=False);self.logger.info(f"Build script input JSON created: {self.config.package_build_inputs_path}")
            log_debug(f"Build inputs JSON content: {json.dumps(bld_in_data,indent=2,sort_keys=True)[:500]}");end_group();return True
        except Exception as e: log_error("BuildInputFail",f"Failed write build inputs JSON: {e}");end_group();return False

    def _determine_build_mode(self, pkg_status: PackageOverallStatus) -> str:
        if pkg_status.pkgbuild_dir_rel_to_workspace:
            mode_dir_name=pkg_status.pkgbuild_dir_rel_to_workspace.parent.name
            if mode_dir_name in ["build","test"]: self.logger.info(f"Build mode for {pkg_status.pkgbase} is '{mode_dir_name}'.");return mode_dir_name
        self.logger.info(f"Defaulting build mode 'nobuild' for {pkg_status.pkgbase}.");return "nobuild"

    def _get_current_pkgbuild_version_from_file(self, pkgbuild_path: Path) -> Tuple[Optional[str], Optional[str]]:
        if not pkgbuild_path.is_file(): self.logger.warning(f"PKGBUILD not found: {pkgbuild_path}."); return None,None
        try:
            c=pkgbuild_path.read_text();pv=(m.group(1) if(m:=re.search(r"^\s*pkgver=([^\s#]+)",c,re.MULTILINE))else None);pr=(m.group(1) if(m:=re.search(r"^\s*pkgrel=([^\s#]+)",c,re.MULTILINE))else "1")
            return pv,pr
        except Exception as e: self.logger.error(f"Error reading version from PKGBUILD {pkgbuild_path}: {e}");return None,None

    def _setup_package_build_environment(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase=pkg_status.pkgbase;op_result.package_specific_build_dir_abs=self.config.package_build_base_dir/f"build-{pkgbase}-{os.urandom(4).hex()}"
        op_result.aur_clone_dir_abs=op_result.package_specific_build_dir_abs/pkgbase
        pkg_artifact_output_dir=self.config.artifacts_dir_base/pkgbase;op_result.log_artifact_subdir=pkg_artifact_output_dir.relative_to(self.config.artifacts_dir_base)
        try:
            self.builder_runner.run(["mkdir","-p",str(op_result.aur_clone_dir_abs)]);self.logger.info(f"Created pkg build dir: {op_result.aur_clone_dir_abs}")
            self.builder_runner.run(["mkdir","-p",str(pkg_artifact_output_dir)])
            aur_repo_url=f"ssh://aur@aur.archlinux.org/{pkgbase}.git";self.logger.info(f"Cloning AUR repo: {aur_repo_url} into {op_result.aur_clone_dir_abs}")
            self.builder_runner.run(["git","clone",aur_repo_url,str(op_result.aur_clone_dir_abs)],check=True) # Ensure clone succeeds
            self.builder_runner.run(["git","status"],cwd=op_result.aur_clone_dir_abs,check=True)
            src_pkg_dir_ws=pkg_status.local_pkgbuild_info.pkgfile_abs_path.parent;self.logger.info(f"Overlaying files from {src_pkg_dir_ws} to {op_result.aur_clone_dir_abs} via rsync")
            self.builder_runner.run(["rsync","-rltvH","--no-owner","--no-group","--exclude=.git","--delete-after",str(src_pkg_dir_ws)+"/",str(op_result.aur_clone_dir_abs)+"/"])
            if not(op_result.aur_clone_dir_abs/".git").is_dir(): op_result.error_message=f".git dir missing in {op_result.aur_clone_dir_abs} post-setup."; self.logger.error(op_result.error_message); return False
            op_result.setup_env_ok=True;return True
        except Exception as e: op_result.error_message=f"Failed setup build env for {pkgbase}: {e}";self.logger.error(op_result.error_message,exc_info=self.config.debug_mode);return False

    def _install_package_dependencies(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pb=pkg_status.pkgbase;self.logger.info(f"Processing deps for {pb}...");pkg_deps_i=build_inputs_data.get(pb,{})
        all_deps=list(set(pkg_deps_i.get("depends",[])+pkg_deps_i.get("makedepends",[])+pkg_deps_i.get("checkdepends",[])))
        clean_deps=sorted(list(set(filter(None,[re.split(r'[<>=!]',d)[0].strip() for d in all_deps if d.strip()]))))
        if not clean_deps: self.logger.info(f"No explicit deps for {pb}.");op_result.dependencies_installed_ok=True;return True
        self.logger.info(f"Installing deps for {pb} via paru: {clean_deps}")
        try: self.builder_runner.run(["paru","-S","--noconfirm","--needed","--norebuild","--sudoloop"]+clean_deps,cwd=BUILDER_HOME,check=True);self.logger.info(f"Deps for {pb} installed.");op_result.dependencies_installed_ok=True;return True
        except subprocess.CalledProcessError as e: op_result.error_message=f"Dep install failed for {pb}. Paru RC: {e.returncode}. Stderr: {e.stderr[:200]}";self.logger.error(op_result.error_message);return False

    def _manage_pkgbuild_versioning_in_clone(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pb=pkg_status.pkgbase;pkgb_f_clone=op_result.aur_clone_dir_abs/"PKGBUILD";new_pv_target,new_pr_target=None,"1"
        if pkg_status.update_source_type=="nvchecker (upstream)" and pkg_status.version_for_update: new_pv_target=pkg_status.version_for_update;op_result.target_version_for_build=f"{new_pv_target}-{new_pr_target}"
        elif pkg_status.update_source_type=="aur" and pkg_status.aur_pkgver: new_pv_target,new_pr_target=pkg_status.aur_pkgver,pkg_status.aur_pkgrel or "1";op_result.target_version_for_build=f"{new_pv_target}-{new_pr_target}"
        loc_nv_toml_clone=op_result.aur_clone_dir_abs/".nvchecker.toml"
        if loc_nv_toml_clone.is_file():
            up_ver_pkg_nv=self.nvchecker.run_nvchecker_for_package_aur_clone(loc_nv_toml_clone)
            if up_ver_pkg_nv:
                self.logger.info(f"Package .nvchecker.toml for {pb} suggests upstream: {up_ver_pkg_nv}")
                if not new_pv_target or self.version_comparator.compare_pkg_versions(new_pv_target,None,up_ver_pkg_nv,None)=="upgrade":
                    self.logger.info(f"Overriding target ver for {pb} to {up_ver_pkg_nv} (pkgrel 1) from pkg's .nvchecker.toml.")
                    new_pv_target,new_pr_target=up_ver_pkg_nv,"1";op_result.target_version_for_build=f"{new_pv_target}-{new_pr_target}"
        if not new_pv_target:
            cur_pv,cur_pr=self._get_current_pkgbuild_version_from_file(pkgb_f_clone);op_result.target_version_for_build=self.version_comparator.get_full_version_string(cur_pv,cur_pr)
            self.logger.info(f"No new ver target for {pb}. PKGBUILD versioning unchanged. Current: {op_result.target_version_for_build}");op_result.pkgbuild_versioned_ok=True;return True
        self.logger.info(f"Targeting ver {new_pv_target}-{new_pr_target} for {pb} in PKGBUILD.")
        try:
            cont=orig_cont=pkgb_f_clone.read_text();cont=re.sub(r"(^\s*pkgver=)([^\s#]+)",rf"\g<1>{new_pv_target}",cont,count=1,flags=re.MULTILINE);cont=re.sub(r"(^\s*pkgrel=)([^\s#]+)",rf"\g<1>{new_pr_target}",cont,count=1,flags=re.MULTILINE)
            if cont!=orig_cont: pkgb_f_clone.write_text(cont);self.logger.info(f"PKGBUILD for {pb} updated.");op_result.changes_made_to_aur_clone_files=True
            else: self.logger.info(f"PKGBUILD for {pb} already reflects target ver or regex miss.")
            op_result.pkgbuild_versioned_ok=True;return True
        except Exception as e: op_result.error_message=f"Failed update PKGBUILD ver for {pb}: {e}";self.logger.error(op_result.error_message,exc_info=self.config.debug_mode);return False

    def _execute_makepkg_and_install(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode not in ["build","test"]:
            self.logger.info(f"Skipping makepkg build for {pkg_status.pkgbase} (mode: {build_mode}).")
            pkgb_f_clone,srci_f_clone=op_result.aur_clone_dir_abs/"PKGBUILD",op_result.aur_clone_dir_abs/".SRCINFO"
            if op_result.changes_made_to_aur_clone_files or \
               (pkgb_f_clone.exists() and srci_f_clone.exists() and pkgb_f_clone.stat().st_mtime > srci_f_clone.stat().st_mtime) or \
               (not srci_f_clone.exists() and pkgb_f_clone.exists()):
                try:
                    self.logger.info(f"Regenerating .SRCINFO for {pkg_status.pkgbase} in mode '{build_mode}'.")
                    srci_res=self.builder_runner.run(["makepkg","--printsrcinfo"],cwd=op_result.aur_clone_dir_abs,check=True)
                    srci_f_clone.write_text(srci_res.stdout);op_result.changes_made_to_aur_clone_files=True # SRCINFO is a change
                except Exception as e_srcinfo: op_result.error_message=f"Failed regenerate .SRCINFO for {pkg_status.pkgbase} in '{build_mode}': {e_srcinfo}";self.logger.error(op_result.error_message);return False
            op_result.makepkg_ran_ok=op_result.local_install_ok=True;return True
        pb=pkg_status.pkgbase;self.logger.info(f"Executing makepkg build for {pb} (mode: {build_mode}). CWD: {op_result.aur_clone_dir_abs}")
        try:
            self.builder_runner.run(["updpkgsums"],cwd=op_result.aur_clone_dir_abs,check=True);op_result.changes_made_to_aur_clone_files=True
            srci_res=self.builder_runner.run(["makepkg","--printsrcinfo"],cwd=op_result.aur_clone_dir_abs,check=True)
            (op_result.aur_clone_dir_abs/".SRCINFO").write_text(srci_res.stdout);op_result.changes_made_to_aur_clone_files=True
            self.builder_runner.run(["makepkg","-Lcs","--noconfirm","--needed","--noprogressbar","--log"],cwd=op_result.aur_clone_dir_abs,check=True);op_result.makepkg_ran_ok=True
            if self.config.debug_mode:
                self.logger.info(f"Dir structure of {op_result.aur_clone_dir_abs} after makepkg for {pb}:")
                tree_log=self.builder_runner.run(["tree","-L","3","-a"],cwd=op_result.aur_clone_dir_abs,check=False,capture_output=True)
                if tree_log.stdout: self.logger.info(f"\n{tree_log.stdout.strip()}")
                if tree_log.stderr: self.logger.warning(f"Tree cmd stderr (after makepkg): {tree_log.stderr.strip()}")
            built_archs=sorted(list(op_result.aur_clone_dir_abs.glob(f"{pb}*.pkg.tar.zst")))or sorted(list(op_result.aur_clone_dir_abs.glob("*.pkg.tar.zst")))
            if not built_archs: raise Exception("No package files (*.pkg.tar.zst) found after makepkg.")
            op_result.built_package_archive_files=[p.resolve() for p in built_archs];self.logger.info(f"Successfully built: {', '.join(p.name for p in op_result.built_package_archive_files)}")
            fin_pv,fin_pr=self._get_current_pkgbuild_version_from_file(op_result.aur_clone_dir_abs/"PKGBUILD");op_result.final_pkgbuild_version_in_clone=self.version_comparator.get_full_version_string(fin_pv,fin_pr)
            self.builder_runner.run(["sudo","pacman","-U","--noconfirm"]+[str(p.name) for p in op_result.built_package_archive_files],cwd=op_result.aur_clone_dir_abs,check=True)
            self.logger.info("Installed built package(s) locally.");op_result.local_install_ok=True;return True
        except Exception as e: op_result.error_message=f"makepkg build process failed for {pb}: {e}";self.logger.error(op_result.error_message,exc_info=self.config.debug_mode);return False

    def _handle_github_release(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode!="build": self.logger.info(f"Skipping GitHub release for {pkg_status.pkgbase} (mode: {build_mode}).");op_result.github_release_ok=True;return True
        pb=pkg_status.pkgbase;ver_tag=op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build
        if not ver_tag: op_result.error_message=f"Cannot create GH release for {pb}: version unknown.";self.logger.error(op_result.error_message);return False
        if not op_result.built_package_archive_files: op_result.error_message=f"Cannot create GH release for {pb}: no built pkg files.";self.logger.error(op_result.error_message);return False
        tag_n,rel_title=f"{pb}-{ver_tag}",f"{pb} {ver_tag}";self.logger.info(f"Handling GH release {tag_n} for {pb} in repo {self.config.github_repo}.")
        gh_env={"GITHUB_TOKEN":self.config.gh_token}
        try:
            view_res=self.builder_runner.run(["gh","release","view",tag_n,"--repo",self.config.github_repo],cwd=op_result.aur_clone_dir_abs,check=False,env_extra=gh_env)
            rel_notes=f"Automated CI release for {pb} {ver_tag}."
            if view_res.returncode!=0: self.logger.info(f"Release {tag_n} not exist. Creating...");self.builder_runner.run(["gh","release","create",tag_n,"--repo",self.config.github_repo,"--title",rel_title,"--notes",rel_notes],cwd=op_result.aur_clone_dir_abs,check=True,env_extra=gh_env)
            else: self.logger.info(f"Release {tag_n} exists. Will update assets.")
            for pkg_arch_abs in op_result.built_package_archive_files: self.builder_runner.run(["gh","release","upload",tag_n,str(pkg_arch_abs),"--repo",self.config.github_repo,"--clobber"],cwd=op_result.aur_clone_dir_abs,check=True,env_extra=gh_env)
            self.logger.info(f"Uploaded assets to GH release {tag_n}.");op_result.github_release_ok=True;return True
        except Exception as e: op_result.error_message=f"GH release failed for {pb} (tag {tag_n}): {e}";self.logger.error(op_result.error_message,exc_info=self.config.debug_mode);return False

    def _commit_and_push_to_aur_repo(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pb=pkg_status.pkgbase;files_to_stage=["PKGBUILD",".SRCINFO"]
        if(op_result.aur_clone_dir_abs/".nvchecker.toml").is_file(): files_to_stage.append(".nvchecker.toml")
        pkg_src_info=build_inputs_data.get(pb,{}).get("sources",[])
        for src_entry in pkg_src_info:
            src_fn_part=src_entry.split('::')[0]
            if not ("://" in src_entry or src_entry.startswith("git+")):
                if(op_result.aur_clone_dir_abs/src_fn_part).is_file():
                    if src_fn_part not in files_to_stage: files_to_stage.append(src_fn_part)
                else: self.logger.warning(f"Local source '{src_fn_part}' for {pb} not in AUR clone. Not staging.")
        self.logger.info(f"Files to stage for AUR commit for {pb}: {files_to_stage}")
        if self.config.debug_mode:
            self.logger.info(f"Dir structure of {op_result.aur_clone_dir_abs} before git add for {pb}:")
            tree_log=self.builder_runner.run(["tree","-L","3","-a"],cwd=op_result.aur_clone_dir_abs,check=False,capture_output=True)
            if tree_log.stdout: self.logger.info(f"\n{tree_log.stdout.strip()}")
        if not(op_result.aur_clone_dir_abs/".git").is_dir(): op_result.error_message=f"AUR clone {op_result.aur_clone_dir_abs} not git repo (missing .git).";self.logger.error(op_result.error_message);return False
        try: self.builder_runner.run(["git","add"]+files_to_stage,cwd=op_result.aur_clone_dir_abs,check=True)
        except subprocess.CalledProcessError as e_add:
            if any(f in str(e_add.stderr)for f in ["PKGBUILD",".SRCINFO"]): op_result.error_message=f"Failed 'git add' critical files for {pb}: {e_add.stderr}";self.logger.error(op_result.error_message);return False
            self.logger.warning(f"'git add' minor issues for {pb}, continuing. Stderr: {e_add.stderr}")
        stat_res=self.builder_runner.run(["git","status","--porcelain"],cwd=op_result.aur_clone_dir_abs,check=True)
        if not stat_res.stdout.strip() and not op_result.changes_made_to_aur_clone_files:
            self.logger.info(f"No git changes to commit to AUR for {pb}.");op_result.git_commit_to_aur_ok=op_result.git_push_to_aur_ok=True;return True
        self.logger.info(f"Git changes detected for {pb}. Committing/pushing to AUR...")
        com_ver_suf=f" (v{op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build or 'unknown'})"
        aur_com_msg=f"CI: Auto update {pb}{com_ver_suf}"
        try:
            self.builder_runner.run(["git","commit","-m",aur_com_msg],cwd=op_result.aur_clone_dir_abs,check=True);op_result.git_commit_to_aur_ok=True
            self.builder_runner.run(["git","push","origin","master"],cwd=op_result.aur_clone_dir_abs,check=True) # Default AUR branch 'master'
            self.logger.info(f"Changes pushed to AUR for {pb}.");op_result.git_push_to_aur_ok=True;return True
        except Exception as e: op_result.error_message=f"Git commit/push to AUR failed for {pb}: {e}";self.logger.error(op_result.error_message,exc_info=self.config.debug_mode);return False

    def _update_workspace_files_from_aur_clone(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        if not op_result.changes_made_to_aur_clone_files:
            self.logger.info(f"No changes in AUR clone for {pkg_status.pkgbase}, skipping workspace update.")
            op_result.workspace_files_updated_ok = True # Skipped but considered okay
            return True

        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Updating workspace files for {pkgbase} from AUR clone {op_result.aur_clone_dir_abs}")

        if not pkg_status.pkgbuild_dir_rel_to_workspace:
            err_msg = f"Cannot determine workspace path for {pkgbase} to copy files back."
            op_result.error_message = (op_result.error_message or "") + f"; {err_msg}"
            self.logger.error(err_msg); return False
        
        target_workspace_pkg_dir_abs = GITHUB_WORKSPACE / pkg_status.pkgbuild_dir_rel_to_workspace
        if not target_workspace_pkg_dir_abs.is_dir():
            err_msg = f"Target workspace directory {target_workspace_pkg_dir_abs} for {pkgbase} does not exist."
            op_result.error_message = (op_result.error_message or "") + f"; {err_msg}"
            self.logger.error(err_msg); return False

        files_to_copy_back = ["PKGBUILD", ".SRCINFO"]
        if (op_result.aur_clone_dir_abs / ".nvchecker.toml").is_file(): files_to_copy_back.append(".nvchecker.toml")

        global_build_inputs_data = {}
        try:
            with open(self.config.package_build_inputs_path, "r") as f: global_build_inputs_data = json.load(f)
        except Exception as e: self.logger.warning(f"Could not read {self.config.package_build_inputs_path} for source file list during workspace update: {e}")

        pkg_sources_from_input = global_build_inputs_data.get(pkgbase, {}).get("sources", [])
        for src_entry in pkg_sources_from_input:
            src_filename_part = src_entry.split('::')[0]
            if not ("://" in src_entry or src_entry.startswith("git+")) and (op_result.aur_clone_dir_abs / src_filename_part).is_file():
                if src_filename_part not in files_to_copy_back: files_to_copy_back.append(src_filename_part)
        
        copied_files_to_workspace_log = []
        all_copied_successfully = True
        for file_name in files_to_copy_back:
            src_file = op_result.aur_clone_dir_abs / file_name
            dest_file = target_workspace_pkg_dir_abs / file_name
            if src_file.is_file():
                try:
                    self.logger.info(f"Copying '{src_file}' to '{dest_file}' for workspace update.")
                    shutil.copy2(src_file, dest_file)
                    copied_files_to_workspace_log.append(str(dest_file.relative_to(GITHUB_WORKSPACE)))
                except Exception as e_copy:
                    err_msg = f"Failed to copy {src_file} to {dest_file}: {e_copy}"
                    op_result.error_message = (op_result.error_message or "") + f"; {err_msg}"; self.logger.error(err_msg); all_copied_successfully = False
            else: self.logger.debug(f"Source file {src_file} for workspace update not found, skipping.")
        
        op_result.workspace_files_updated_ok = all_copied_successfully
        if all_copied_successfully and copied_files_to_workspace_log:
            self.logger.info(f"Successfully updated files in workspace for {pkgbase}: {copied_files_to_workspace_log}")
            self.config.updated_pkgbases_in_workspace.add(pkgbase) # Track for commit message
        elif not all_copied_successfully:
             self.logger.error(f"One or more files failed to copy to workspace for {pkgbase}.")
        return all_copied_successfully

    def _collect_package_artifacts(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase; pkg_artifact_output_dir_abs = self.config.artifacts_dir_base / pkgbase
        self.logger.info(f"Collecting artifacts for {pkgbase} from {op_result.aur_clone_dir_abs} to {pkg_artifact_output_dir_abs}...")
        def _try_copy(src_p: Path, dest_d: Path, dest_rel_p: Optional[Path] = None):
            if src_p.is_file():
                try:
                    final_dest=dest_d/(dest_rel_p if dest_rel_p else src_p.name);final_dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src_p,final_dest)
                    self.logger.debug(f"Copied artifact: {src_p.name} to {final_dest}")
                except Exception as e: self.logger.warning(f"Could not copy artifact {src_p}: {e}")
            else: self.logger.debug(f"Artifact source file not found, skipping: {src_p}")
        try:
            for fn in ["PKGBUILD",".SRCINFO",".nvchecker.toml"]: _try_copy(op_result.aur_clone_dir_abs/fn, pkg_artifact_output_dir_abs)
            for log_f_abs in op_result.aur_clone_dir_abs.glob("*.log"): _try_copy(log_f_abs, pkg_artifact_output_dir_abs)
            pkg_build_artifacts_dir = op_result.aur_clone_dir_abs/"pkg"
            if pkg_build_artifacts_dir.is_dir():
                for item_type in [".BUILDINFO",".MTREE",".PKGINFO"]:
                    for found_f in pkg_build_artifacts_dir.rglob(f"*{item_type}"): # Includes subdirs
                        rel_p_clone=found_f.relative_to(op_result.aur_clone_dir_abs);_try_copy(found_f,pkg_artifact_output_dir_abs,dest_rel_p=rel_p_clone)
            self.logger.info(f"Artifact collection for {pkgbase} finished.");return True
        except Exception as e_art_main:
            err_det=f"Error during artifact collection for {pkgbase}: {e_art_main}";op_result.error_message=(op_result.error_message+"; " if op_result.error_message else "")+err_det
            self.logger.warning(err_det,exc_info=self.config.debug_mode);return False

    def _cleanup_package_build_environment(self, op_result: BuildOpResult) -> bool:
        if op_result.package_specific_build_dir_abs and op_result.package_specific_build_dir_abs.exists():
            self.logger.info(f"Cleaning up build directory: {op_result.package_specific_build_dir_abs}")
            try: self.runner.run(["rm","-rf",str(op_result.package_specific_build_dir_abs)],check=True,run_as_user="root");return True
            except Exception as e:
                err_det=f"Failed cleanup pkg build dir {op_result.package_specific_build_dir_abs}: {e}";op_result.error_message=(op_result.error_message+"; " if op_result.error_message else "")+err_det
                self.logger.warning(err_det);return False
        self.logger.debug(f"Build dir {op_result.package_specific_build_dir_abs} not found/set, skipping cleanup.");return True

    def _perform_package_build_operations(self, pkg_status: PackageOverallStatus, build_mode: str, build_inputs_data: Dict[str, Any]) -> BuildOpResult:
        pb=pkg_status.pkgbase;start_group(f"Processing Package: {pb} (Mode: {build_mode})");op_result=BuildOpResult(package_name=pb)
        try:
            if not self._setup_package_build_environment(pkg_status,op_result): raise Exception(op_result.error_message or "Env setup failed.")
            if not self._install_package_dependencies(pkg_status,op_result,build_inputs_data): raise Exception(op_result.error_message or "Dep install failed.")
            if not self._manage_pkgbuild_versioning_in_clone(pkg_status,op_result): raise Exception(op_result.error_message or "PKGBUILD versioning failed.")
            if not self._execute_makepkg_and_install(pkg_status,op_result,build_mode): raise Exception(op_result.error_message or "Makepkg/install failed.")
            if not self._handle_github_release(pkg_status,op_result,build_mode): self.logger.warning(f"GH Release issue for {pb}: {op_result.error_message or 'Unknown GH Release issue'}")
            if not self._commit_and_push_to_aur_repo(pkg_status,op_result,build_inputs_data): raise Exception(op_result.error_message or "AUR commit/push failed.")
            
            # Update workspace files for auto-commit action
            if not self._update_workspace_files_from_aur_clone(pkg_status, op_result):
                 self.logger.warning(f"Updating files in workspace for {pb} had issues: {op_result.error_message or 'Unknown workspace update issue'}")
            # Note: op_result.workspace_files_updated_ok is set by the above call

            op_result.success=(op_result.setup_env_ok and op_result.dependencies_installed_ok and op_result.pkgbuild_versioned_ok and
                               op_result.makepkg_ran_ok and op_result.local_install_ok and op_result.git_commit_to_aur_ok and op_result.git_push_to_aur_ok)
            if op_result.success: self.logger.info(f"Successfully processed and updated AUR for package {pb}.")
            else: self.logger.warning(f"Package {pb} processed, but AUR update op_result.success is False. Error: {op_result.error_message}")
        except Exception as e_main_build_op:
            self.logger.error(f"Main build op for {pb} failed critically: {e_main_build_op}",exc_info=self.config.debug_mode)
            if not op_result.error_message: op_result.error_message=str(e_main_build_op)
            op_result.success=False
        finally:
            self._collect_package_artifacts(pkg_status,op_result)
            self._cleanup_package_build_environment(op_result)
        end_group();return op_result

    def _write_summary_to_file(self, overall_statuses: List[PackageOverallStatus]):
        if not self.config.github_step_summary_file: self.logger.info("No GITHUB_STEP_SUMMARY_FILE_PATH, skipping summary."); return
        start_group("Generating Workflow Summary")
        try:
            with open(self.config.github_step_summary_file,"w",encoding="utf-8") as f:
                f.write("## Arch Package Update Summary\n\n| Package | Version (Local) | Status | Details | AUR Link | Artifacts Context |\n|---|---|---|---|---|---|\n")
                if not self.build_operation_results and not any(s.needs_update or s.local_is_ahead or s.comparison_errors for s in overall_statuses):
                     f.write("| *No updates or significant status changes found* | - | - | - | - | - |\n")
                build_res_map={res.package_name:res for res in self.build_operation_results}
                for stat in overall_statuses:
                    pb=stat.pkgbase;loc_v_s=self.version_comparator.get_full_version_string(stat.local_pkgbuild_info.pkgver if stat.local_pkgbuild_info else None,stat.local_pkgbuild_info.pkgrel if stat.local_pkgbuild_info else None)or"N/A"
                    aur_l=f"[{pb}](https://aur.archlinux.org/packages/{pb})";stat_txt,det_txt,logs_link_ctx="Up-to-date","","N/A"
                    if(build_res:=build_res_map.get(pb)):
                        ver_sh=build_res.final_pkgbuild_version_in_clone or build_res.target_version_for_build or "Unknown"
                        if build_res.success:
                            stat_txt=f"✅ AUR Updated: v{ver_sh}"
                            if build_res.github_release_ok: det_txt+="GH Release. "
                            if build_res.workspace_files_updated_ok and pb in self.config.updated_pkgbases_in_workspace : det_txt+="Workspace files updated for auto-commit. "
                        else: stat_txt=f"❌ AUR Update Failed: v{ver_sh}";det_txt=f"<small>{(build_res.error_message or 'Unknown error').replace('|','-').replace(chr(10),'<br>')}</small>"
                        if build_res.log_artifact_subdir and self.config.github_run_id: logs_link_ctx=f"`{build_res.log_artifact_subdir.name}/` in `build-artifacts-{self.config.github_run_id}.zip`"
                        elif build_res.log_artifact_subdir: logs_link_ctx=f"`{build_res.log_artifact_subdir.name}/` in artifact zip"
                    elif stat.needs_update: stat_txt,det_txt=f"⚠️ Update Pending: to v{stat.version_for_update}",f"Source: {stat.update_source_type}."
                    elif stat.local_is_ahead: stat_txt,det_txt="ℹ️ Local Ahead",f"Local ({loc_v_s}) > sources."
                    elif stat.comparison_errors: stat_txt,det_txt=f"❗ Error","; ".join(stat.comparison_errors)
                    f.write(f"| **{pb}** | `{loc_v_s}` | {stat_txt} | {det_txt} | {aur_l} | {logs_link_ctx} |\n")
            self.logger.info(f"Workflow summary written to {self.config.github_step_summary_file}")
        except Exception as e: log_error("SummaryWriteFail",f"Failed to write workflow summary: {e}")
        end_group()

    def _generate_commit_message_file(self):
        commit_msg_file_path = GITHUB_WORKSPACE / COMMIT_MESSAGE_FILENAME
        commit_message_generated = False
        if self.config.updated_pkgbases_in_workspace:
            pkg_list_str = ', '.join(sorted(list(self.config.updated_pkgbases_in_workspace)))
            commit_msg_title = f"chore: Auto-update package sources for: {pkg_list_str}"
            commit_msg_body = (
                "The following packages had their source files (PKGBUILD, .SRCINFO, etc.) "
                "updated in the repository by the CI workflow:\n\n"
            )
            for pkg_name in sorted(list(self.config.updated_pkgbases_in_workspace)):
                commit_msg_body += f"- {pkg_name}\n"
            
            commit_message_content = f"{commit_msg_title}\n\n{commit_msg_body}"
            try:
                with open(commit_msg_file_path, "w") as f: f.write(commit_message_content)
                self.logger.info(f"Generated commit message for auto-commit action: {commit_msg_file_path}")
                log_notice("CommitMessageGenerated", f"Commit message for auto-commit action written to {commit_msg_file_path.name}")
                commit_message_generated = True
            except Exception as e:
                log_error("CommitMessageFail", f"Failed to write commit message file: {e}")
        else:
            self.logger.info("No packages updated in workspace, no specific commit message generated.")
            # Create an empty file so the workflow step for commit_options has a target
            # The auto-commit action should ideally not commit if there are no changes.
            try:
                commit_msg_file_path.write_text("") # Write empty if no changes
                self.logger.info(f"Created empty commit message file as no workspace changes: {commit_msg_file_path}")
            except Exception as e:
                 log_error("CommitMessageFail", f"Failed to write empty commit message file: {e}")
        
        # Set GHA outputs for commit message file status
        print(f"::set-output name=commit_message_file_exists::{str(commit_msg_file_path.exists()).lower()}")
        if commit_msg_file_path.exists() and commit_msg_file_path.read_text().strip():
            print(f"::set-output name=commit_message_file_non_empty::true")
        else:
            print(f"::set-output name=commit_message_file_non_empty::false")


    def run_workflow(self):
        log_notice("WorkflowStart", "Arch Package Management Workflow Starting...")
        if not self._initial_environment_setup():
            log_error("Fatal","Environment setup failed. Exiting.");
            if self.config.github_step_summary_file: self.config.github_step_summary_file.write_text("|**SETUP**|N/A|❌ Failure: Env setup|-|-|Check Logs|\n", "a")
            return 1

        loc_pkg_data=self.pkgbuild_parser.fetch_all_local_pkgbuild_data(self.config.pkgbuild_files_root_in_workspace)
        if not loc_pkg_data: log_warning("NoLocalData","No local PKGBUILD data found.")
        aur_pkg_data=self.aur_fetcher.fetch_data_for_maintainer(self.config.aur_maintainer_name)
        nv_oldver_in={};all_rel_pb=set(loc_pkg_data.keys())|set(aur_pkg_data.keys())
        for pb in all_rel_pb:
            ver_to_use=(aur_pkg_data.get(pb,{}).get("aur_pkgver")or(loc_pkg_data.get(pb).pkgver if loc_pkg_data.get(pb)else None))
            if ver_to_use: nv_oldver_in[pb]={"version":ver_to_use}
        nv_glob_res=self.nvchecker.run_global_nvchecker(self.config.pkgbuild_files_root_in_workspace,nv_oldver_in)
        all_pkg_stats=self._analyze_package_statuses(loc_pkg_data,aur_pkg_data,nv_glob_res)
        try:
            self.config.package_status_report_path.parent.mkdir(parents=True,exist_ok=True)
            self.config.package_status_report_path.write_text(json.dumps([dataclasses.asdict(s) for s in all_pkg_stats],indent=2,default=str))
            self.runner.run(["cp",str(self.config.package_status_report_path),str(self.config.artifacts_dir_base/self.config.package_status_report_path.name)],check=False)
            log_notice("StatusArtifact",f"Package status report saved to artifacts: {self.config.package_status_report_path.name}")
        except Exception as e: log_warning("StatusArtifactFail",f"Failed to save status report artifact: {e}")

        pkgs_need_build=[s for s in all_pkg_stats if s.needs_update and s.is_update_candidate and s.local_pkgbuild_info and s.pkgbuild_dir_rel_to_workspace]
        if not pkgs_need_build: log_notice("NoUpdatesToBuild","No packages require build actions.");self._write_summary_to_file(all_pkg_stats); self._generate_commit_message_file(); return 0
        log_notice("UpdatesFound",f"Found {len(pkgs_need_build)} package(s) requiring build actions.")

        if not self._prepare_inputs_for_build_script(pkgs_need_build):
            log_error("Fatal","Failed to prepare inputs for build script. Exiting.");self._write_summary_to_file(all_pkg_stats); self._generate_commit_message_file(); return 1
        bld_in_json_cont=json.loads(self.config.package_build_inputs_path.read_text())
        
        overall_workflow_success=True
        for pkg_stat_build in pkgs_need_build:
            build_mode=self._determine_build_mode(pkg_stat_build)
            cur_pkg_bld_in=bld_in_json_cont.get(pkg_stat_build.pkgbase,{})
            build_op_res=self._perform_package_build_operations(pkg_stat_build,build_mode,cur_pkg_bld_in)
            self.build_operation_results.append(build_op_res)
            if not build_op_res.success: overall_workflow_success=False;log_error("PackageBuildFail",f"AUR update/build operation failed for {pkg_stat_build.pkgbase}.")
        
        self._write_summary_to_file(all_pkg_stats)
        self._generate_commit_message_file() # Generate commit message based on workspace changes

        if not overall_workflow_success: log_error("WorkflowEndFail","One or more package AUR update/build operations failed.");return 1
        log_notice("WorkflowEndSuccess","All package management tasks completed successfully.");return 0

def main():
    try:
        cfg=Config(aur_maintainer_name=os.environ["AUR_MAINTAINER_NAME"],github_repo=os.environ["GITHUB_REPO_OWNER_SLASH_NAME"],
                   pkgbuild_files_root_in_workspace=Path(os.environ["PKGBUILD_FILES_ROOT"]).resolve(),git_commit_user_name=os.environ["GIT_COMMIT_USER_NAME"],
                   git_commit_user_email=os.environ["GIT_COMMIT_USER_EMAIL"],gh_token=os.environ["GH_TOKEN_FOR_RELEASES_AND_NVCHECKER"])
    except KeyError as e: log_error("ConfigFatal",f"Missing critical env var: {e}.");sys.exit(2)
    except Exception as e_cfg: log_error("ConfigFatal",f"Error initializing config: {e_cfg}");sys.exit(2)
    manager=ArchPackageManager(cfg);sys.exit(manager.run_workflow())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,format="%(levelname)s: %(message)s",stream=sys.stderr)
    main()
