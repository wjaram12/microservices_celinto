"""
Motor de plantillas (Jinja2) del panel de administración.

Las plantillas viven en app/templates/, una carpeta por view:
    templates/base.html               layout común (estilos, login, nav, helpers JS)
    templates/prompts/index.html      página de la view adm_prompts
    templates/consumidores/index.html página de la view adm_consumidores
    templates/procesadores/index.html página de la view adm_procesadores
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

plantillas = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
