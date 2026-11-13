import os
import re
from typing import Any, Dict, Optional

import yaml
from jinja2 import Environment, FileSystemLoader

_token_re = re.compile(r'\$\{([^}]+)\}')

def _convert_dollar_tokens_to_jinja(text: str) -> str:
    """Convert ${TOKEN} to {{ TOKEN }} for Jinja2 compatibility."""
    return _token_re.sub(r'{{ \1 }}', text)

def j2_yaml_loader(path: str, replacements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Load a YAML config file rendered as a Jinja2 template.
    Supports both `${TOKEN}` and `{{ TOKEN }}` placeholders.
    Also supports `{% include ... %}` from sibling directories.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    base_dir = os.path.dirname(path)
    os.path.basename(path)

    # Define search paths for includes
    search_paths = [
        base_dir,
        os.path.abspath(os.path.join(base_dir, '..')),       # e.g., beamlines/
        os.path.abspath(os.path.join(base_dir, '../../')),   # e.g., scheduler_config/
    ]
    
    # Jinja2 template environment
    env = Environment(
        loader=FileSystemLoader(search_paths),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # Load raw text and convert ${TOKEN} to {{ TOKEN }}
    with open(path, 'r') as f:
        raw_text = f.read()
    jinja_compatible_text = _convert_dollar_tokens_to_jinja(raw_text)

    # Render with Jinja2
    rendered = env.from_string(jinja_compatible_text).render(replacements or {})
    # print("----- Rendered YAML -----")
    # print(rendered)

    # Parse YAML
    return yaml.safe_load(rendered)

