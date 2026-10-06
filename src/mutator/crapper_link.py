"""Import crapper from the environment or the sibling checkout.

Function names and namespaces come from crapper so `.metrics/mutate` joins
the same operations uml-viewer joins to `.metrics/crap.edn`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _sibling_src() -> Path:
    """crapper's src beside the mutator checkout, or beside the nearest folder that holds it.

    A mutation worker copies the checkout into the checkout's own target
    folder, so from a worker the crapper found is the one beside the checkout
    (issue #8).
    """

    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / "crapper" / "src" / "crapper").is_dir():
            return folder / "crapper" / "src"
    return here.parents[3] / "crapper" / "src"


def ensure_crapper():
    try:
        import crapper
    except ImportError:
        sibling = _sibling_src()
        if not (sibling / "crapper").is_dir():
            raise ImportError(
                "crapper is not installed and was not found at "
                f"{sibling}. Clone github.com/unclebob/crapper next to mutator."
            ) from None
        sys.path.insert(0, str(sibling))
        import crapper
    import crapper.coverage
    import crapper.discover
    import crapper.languages
    import crapper.languages.treesitter
    import crapper.runners

    return crapper
