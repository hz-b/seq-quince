import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

def is_valid_logging_level(level):
    """Check if the given integer is a valid logging level."""
    return level in [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]

def set_valid_logging_level(level):
    """
    Converts mbbo record values 0..5 to logging levels and checks if valid.
    If level is not an integer, it prints an error message.
    TODO: 0 - no change
    """
    try:
        level = int(level)
    except (ValueError, TypeError):
        print(f"Invalid logging level (not an integer): {level}")
        return

    scaled_level = level * 10
    if is_valid_logging_level(scaled_level):
        logging.getLogger().setLevel(scaled_level)  # Set logger level using the scaled value
        logger.info(f"Logging level set to {logging.getLevelName(scaled_level)}")
    else:
        logger.error(f"Invalid logging level ({level})!")

def get_seq_quince_config_dir():
    """
    Returns the absolute path from SEQ_QUINCE_CONFIG_DIR if defined and is a valid directory,
    otherwise returns None.
    """
    dir_path = os.environ.get("SEQ_QUINCE_CONFIG_DIR")
    if dir_path and os.path.isdir(dir_path):
        return os.path.abspath(dir_path)
    return None

def get_scheduler_config_dir(user_dir: Optional[str]=None) -> str:
    """
    Return the scheduler configuration directory, optionally under user_dir.

    An existing SEQ_QUINCE_CONFIG_DIR supplies the base for
    scheduler_config/beamlines/user_dir. Otherwise use the module-relative
    ../config/beamlines/user_dir directory.
    """

    if user_dir is None:
        user_dir = ""

    seq_quince_config_dir = get_seq_quince_config_dir()
    if seq_quince_config_dir is not None:
        # If the environment variable is set, override the default
        return os.path.normpath(os.path.join(seq_quince_config_dir, 'scheduler_config', 'beamlines', user_dir))
    
    # Fallback to the original default if the environment variable is not set or invalid
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(current_file_dir, '..', 'config', 'beamlines', user_dir))

def merge_replacements(config: dict, cli_replacements: Optional[dict]) -> None:
    if cli_replacements is None:
        return  # Nothing to do
    # Ensure config["replacements"] is a dict
    if "replacements" not in config or not isinstance(config["replacements"], dict):
        config["replacements"] = dict(cli_replacements)  # Make a copy
    else:
        # Merge in-place, CLI has precedence
        config["replacements"].update(cli_replacements)
