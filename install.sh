#!/usr/bin/env bash
#
# install.sh - one-click installer for x-use.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ihuzaifashoukat/x-use/main/install.sh | bash
#   ./install.sh [--dir PATH] [--dev] [--update] [--skip-browser] [--with-system-deps]
#
# What it does:
#   1. Preflight: git and Python >= 3.10 must be present.
#   2. Clone the repo (or reuse/update an existing clone, or install in
#      place when run from inside the repo).
#   3. Bootstrap uv locally if needed and synchronize .venv from uv.lock.
#   4. Install Chromium for the configured Patchright or Playwright backend.
#   5. Create missing sample config and run doctor without overwriting config.
#
# The script is idempotent: re-running it reuses the clone and the venv.
# Administrator changes are opt-in with --with-system-deps on Linux. uv and
# browser drivers use their normal user caches; no global Python packages change.

set -euo pipefail

# XUSE_REPO_URL overrides the clone source (forks, mirrors, testing).
readonly REPO_URL="${XUSE_REPO_URL:-https://github.com/ihuzaifashoukat/x-use.git}"
readonly DEFAULT_DIR="x-use"
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=10

INSTALL_DIR=""
DEV_INSTALL=0
UPDATE=0
SKIP_BROWSER=0
WITH_SYSTEM_DEPS=0

# --- output helpers -----------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

info() { printf '%s==>%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%swarn:%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

trap 'die "installation failed at line ${LINENO} - re-run with bash -x install.sh for a trace."' ERR

usage() {
    cat <<'EOF'
install.sh - one-click installer for x-use.

Usage:
  curl -fsSL https://raw.githubusercontent.com/ihuzaifashoukat/x-use/main/install.sh | bash
  ./install.sh [--dir PATH] [--dev] [--update] [--skip-browser] [--with-system-deps]

Options:
  --dir PATH   Install into PATH instead of ./x-use
  --dev        Install with dev extras (pytest) for contributors
  --update     git pull --ff-only an existing checkout before installing
  --skip-browser       Reuse an already-installed matching browser
  --with-system-deps   Allow Linux OS-package installation (may request sudo)
  -h, --help   Show this help
EOF
    exit "${1:-0}"
}

# --- arguments ----------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            [[ $# -ge 2 ]] || die "--dir requires a path argument."
            INSTALL_DIR="$2"; shift 2 ;;
        --dir=*)
            INSTALL_DIR="${1#*=}"; shift ;;
        --dev)
            DEV_INSTALL=1; shift ;;
        --update)
            UPDATE=1; shift ;;
        --skip-browser)
            SKIP_BROWSER=1; shift ;;
        --with-system-deps)
            WITH_SYSTEM_DEPS=1; shift ;;
        -h|--help)
            usage 0 ;;
        *)
            die "unknown option '$1' (try --help)." ;;
    esac
done

# --- preflight ------------------------------------------------------------------

command -v git >/dev/null 2>&1 || die "git is not installed (or not on PATH)."

PYTHON=""
for candidate in python3 python; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    if "${candidate}" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)" 2>/dev/null; then
        PYTHON="${candidate}"
        break
    fi
done
[[ -n "${PYTHON}" ]] || die "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ not found. Install it from https://www.python.org/downloads/ and re-run."

PY_VERSION="$("${PYTHON}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
info "git $(git --version | awk '{print $3}'), python ${PY_VERSION} - preflight OK."

# --- repo: clone / update / in-place ---------------------------------------------

if [[ -z "${INSTALL_DIR}" && -f "pyproject.toml" ]] && grep -q '^name = "x-use' pyproject.toml 2>/dev/null; then
    # Running from inside the repo: install in place.
    INSTALL_DIR="$(pwd)"
fi
INSTALL_DIR="${INSTALL_DIR:-${DEFAULT_DIR}}"

if [[ -f "${INSTALL_DIR}/pyproject.toml" ]] && grep -q '^name = "x-use' "${INSTALL_DIR}/pyproject.toml" 2>/dev/null; then
    if [[ "${UPDATE}" -eq 1 ]]; then
        info "Updating existing clone in ${INSTALL_DIR} ..."
        git -C "${INSTALL_DIR}" pull --ff-only || die "git pull failed; the existing checkout was not updated."
    else
        info "Existing x-use checkout found in ${INSTALL_DIR} - reusing it (pass --update to pull latest)."
    fi
else
    if [[ -e "${INSTALL_DIR}" ]]; then
        die "'${INSTALL_DIR}' exists but is not an x-use checkout. Pick another --dir or remove it."
    fi
    info "Cloning ${REPO_URL} -> ${INSTALL_DIR} ..."
    git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
    [[ -f "${INSTALL_DIR}/pyproject.toml" ]] && grep -q '^name = "x-use' "${INSTALL_DIR}/pyproject.toml" 2>/dev/null \
        || die "the clone does not look like x-use v2 (missing pyproject.toml). The default branch may predate the v2 merge - clone the right branch or set XUSE_REPO_URL."
fi

# --- shared uv/browser setup -----------------------------------------------------------

INSTALL_DIR="$(cd "${INSTALL_DIR}" && pwd)"
if [[ -z "${X_USE_HOME:-}" ]]; then
    DATA_BASE="${XDG_DATA_HOME:-${HOME:?HOME is required to choose a data directory}/.local/share}"
    X_USE_HOME="${DATA_BASE%/}/x-use"
fi
if command -v cygpath >/dev/null 2>&1; then
    X_USE_HOME="$(cygpath -w "${X_USE_HOME}")"
fi
export X_USE_HOME
SETUP_SCRIPT="${INSTALL_DIR}/scripts/setup_uv.py"
[[ -f "${SETUP_SCRIPT}" ]] || die "the checkout is missing scripts/setup_uv.py."
SETUP_ARGS=("${SETUP_SCRIPT}")
[[ "${DEV_INSTALL}" -eq 0 ]] || SETUP_ARGS+=(--dev)
[[ "${SKIP_BROWSER}" -eq 0 ]] || SETUP_ARGS+=(--skip-browser)
[[ "${WITH_SYSTEM_DEPS}" -eq 0 ]] || SETUP_ARGS+=(--with-system-deps)
info "Setting up uv, x-use and the configured browser ..."
"${PYTHON}" "${SETUP_ARGS[@]}"
BIN_DIR="${INSTALL_DIR}/.venv/bin"
[[ -x "${BIN_DIR}/x-use" ]] || BIN_DIR="${INSTALL_DIR}/.venv/Scripts"
XUSE_BIN="${BIN_DIR}/x-use"
[[ -x "${XUSE_BIN}" ]] || XUSE_BIN="${BIN_DIR}/x-use.exe"
[[ -x "${XUSE_BIN}" ]] || die "setup finished but the x-use command was not found in ${BIN_DIR}."

# --- done -------------------------------------------------------------------------------

# Claude Desktop on Windows needs a C:\... path; cygpath converts when available.
MCP_BIN="$(cygpath -w "${XUSE_BIN}" 2>/dev/null || printf '%s' "${XUSE_BIN}")"
MCP_CONFIG="$("${PYTHON}" "${INSTALL_DIR}/scripts/mcp_config.py" "${MCP_BIN}" "${X_USE_HOME}")"
INSTALL_DIR_Q="$(printf '%q' "${INSTALL_DIR}")"
DATA_HOME_Q="$(printf '%q' "${X_USE_HOME}")"

printf '\n%sx-use is installed.%s\n' "${C_BOLD}" "${C_RESET}"
cat <<EOF

Next steps:
  1. cd -- ${INSTALL_DIR_Q}
  2. export X_USE_HOME=${DATA_HOME_Q}
  3. "${XUSE_BIN}" init      # interactive wizard: account, cookies, LLM keys
  4. "${XUSE_BIN}" doctor    # re-check until every row is PASS/SKIP
  5. "${XUSE_BIN}" run       # or connect an MCP client (below)

MCP client config (e.g. claude_desktop_config.json):
${MCP_CONFIG}

Docs: README.md, BEST_PRACTICES.md, docs/CONFIG_REFERENCE.md
EOF
