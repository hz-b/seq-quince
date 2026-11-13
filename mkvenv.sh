# This script sets up or activates a Python virtual environment in the .venv directory,
# installs the seq_quince package and modifies the shell prompt to indicate the (development) environment.
# It must be sourced to affect the current shell:
#
#   source mkvenv.sh
#
# Optional:
#   export SEQ_QUINCE_REPO_ROOT=/path/to/seq_quince

# Resolve the seq_quince repo root from the script location, but allow an override for unusual setups.
_mkvenv_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_mkvenv_repo_root="${SEQ_QUINCE_REPO_ROOT:-$_mkvenv_script_dir}"
_seq_quince_old_pwd="$PWD"

if [ ! -f "$_mkvenv_repo_root/pyproject.toml" ] && [ ! -f "$_mkvenv_repo_root/setup.py" ]; then
    echo "mkvenv.sh: cannot find pyproject.toml or setup.py in: $_mkvenv_repo_root" >&2
    echo "Set SEQ_QUINCE_REPO_ROOT to the seq_quince checkout root, or source the script by absolute path." >&2
    return 1 2>/dev/null || exit 1
fi

cd "$_mkvenv_repo_root"

export VIRTUAL_ENV_DISABLE_PROMPT=1

# Function to clean the shell prompt
add_mdlr_to_prompt() {
    # Check if "MDLR" is already in PS1
    if [[ "$PS1" != *"MDLR "* ]]; then
        # Add "MDLR " to the beginning of PS1, preserving escape sequences
        PS1="MDLR $PS1"
    fi
}


# Check if .venv directory exists
if [ -d ".venv" ]; then
    # .venv exists
    # Check if a virtual environment is currently activated
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        # VIRTUAL_ENV is non-empty
        echo "Virtual environment is currently activated: $VIRTUAL_ENV"
        echo "Updating seq_quince package"
        pip uninstall seq_quince -y
        pip install -e .[dev]
    else
        # VIRTUAL_ENV is empty
        echo "No virtual environment is activated."
        echo "Activating .venv"
        source .venv/bin/activate
    fi
else
    # .venv does not exist
    echo "Creating virtual environment and installing packages"
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install build wheel
    pip install -e .[dev]
fi

cd "$_seq_quince_old_pwd"
# Update the shell prompt to indicate the seq_quince development environment
add_mdlr_to_prompt
