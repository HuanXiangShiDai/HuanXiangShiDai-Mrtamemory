"""huanzao —— 从一段语料长出一张会自己想起来、会自己改自己的记忆图。

只有点和边：记忆、元素、规则、思考、人格都在同一张图上。
零第三方依赖，只用标准库。
"""

from .config import AXIOMS, ALIASES, CENTER_BODY, PERSONA_NAME

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "PERSONA_NAME",
    "CENTER_BODY",
    "ALIASES",
    "AXIOMS",
]
