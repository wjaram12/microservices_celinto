"""
Construcción del HTML JSF/PrimeFaces que devuelve el servidor mock.

El objetivo es que el HTML tenga EXACTAMENTE los selectores que el parser del
scraper (scraper.py) busca, para que el scraper real corra sin cambios:

  - <input name="javax.faces.ViewState" value="...">
  - LISTADO:  div#formPrincipal:tablaTitulado
              > tbody#formPrincipal:tablaTitulado_data
              > tr[data-ri] con 2 <td> (identificación, nombres)
              y un link cuyo id casa con  formPrincipal:tablaTitulado:{ri}:(j_idt\\d+)
  - DETALLE:  table.ui-panelgrid que contiene "Identifica" y SIN inputs/botones
              (filas clave/valor) + div[id$="tablaAplicaciones"] con <thead>/<tbody>
              precedidos por un <h4> de categoría + button#formPrincipal:btnInfoConsulta
  - ERROR:    span.ui-messages-error-detail con el texto del mensaje
"""
import html

# Sufijo j_idt del command link "Ver Información". En SENESCYT real se regenera
# entre despliegues; el scraper lo extrae por regex. Aquí es fijo y conocido.
CMD_ID = "j_idt37"


def _doc(cuerpo: str, view_state: str) -> str:
    """Envuelve el cuerpo en un documento con el form y el ViewState."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><title>Consulta de Títulos - SENESCYT</title></head>
<body>
<form id="formPrincipal" name="formPrincipal" method="post">
<input type="hidden" name="formPrincipal" value="formPrincipal">
{cuerpo}
<input type="hidden" name="javax.faces.ViewState" id="j_id1:javax.faces.ViewState:0"
       value="{html.escape(view_state)}">
</form>
</body>
</html>"""


def pagina_inicial(view_state: str) -> str:
    """Página inicial: el formulario de búsqueda + la imagen del captcha."""
    cuerpo = """
<h2>Consulta de títulos registrados</h2>
<label>Identificación</label>
<input type="text" name="formPrincipal:identificacion" value="">
<label>Apellidos</label>
<input type="text" name="formPrincipal:apellidos" value="">
<img id="formPrincipal:capImg" src="/consulta-titulos-web/Captcha.jpg" alt="captcha">
<input type="text" name="formPrincipal:captchaSellerInput" value="">
<button type="submit" name="formPrincipal:boton-buscar" value="formPrincipal:boton-buscar">
  Buscar
</button>
"""
    return _doc(cuerpo, view_state)


def mensaje_error(view_state: str, texto: str) -> str:
    """Mensaje de PrimeFaces (p. ej. captcha incorrecto)."""
    cuerpo = f"""
<div class="ui-messages ui-widget" id="formPrincipal:messages">
  <div class="ui-messages-error">
    <span class="ui-messages-error-summary">Error</span>
    <span class="ui-messages-error-detail">{html.escape(texto)}</span>
  </div>
</div>
""" + _formulario_busqueda()
    return _doc(cuerpo, view_state)


def _formulario_busqueda() -> str:
    return """
<label>Identificación</label>
<input type="text" name="formPrincipal:identificacion" value="">
<label>Apellidos</label>
<input type="text" name="formPrincipal:apellidos" value="">
<img id="formPrincipal:capImg" src="/consulta-titulos-web/Captcha.jpg" alt="captcha">
<input type="text" name="formPrincipal:captchaSellerInput" value="">
<button type="submit" name="formPrincipal:boton-buscar" value="formPrincipal:boton-buscar">
  Buscar
</button>
"""


def sin_resultados(view_state: str) -> str:
    """Datatable vacío: el parser lo lee como 'sin resultados'."""
    cuerpo = f"""
<div id="formPrincipal:tablaTitulado" class="ui-datatable ui-widget">
  <table>
    <tbody id="formPrincipal:tablaTitulado_data" class="ui-datatable-data">
      <tr class="ui-widget-content ui-datatable-empty-message">
        <td colspan="2">No se encontraron resultados.</td>
      </tr>
    </tbody>
  </table>
</div>
"""
    return _doc(cuerpo, view_state)


def listado(view_state: str, personas) -> str:
    """
    Listado de personas. `personas` = lista de (ri, cedula, nombres).
    Cada fila lleva el command link 'Ver Información' con el id que el scraper
    extrae por regex y reusa en ver_titulos.
    """
    filas = []
    for ri, cedula, nombres in personas:
        cmd = f"formPrincipal:tablaTitulado:{ri}:{CMD_ID}"
        filas.append(f"""
      <tr data-ri="{ri}" class="ui-widget-content">
        <td>{html.escape(cedula)}</td>
        <td>{html.escape(nombres)}</td>
        <td>
          <a id="{cmd}" name="{cmd}" href="#"
             class="ui-commandlink">Ver Información</a>
        </td>
      </tr>""")
    cuerpo = f"""
<div id="formPrincipal:tablaTitulado" class="ui-datatable ui-widget">
  <table>
    <thead><tr><th>Identificación</th><th>Nombres</th><th>Acción</th></tr></thead>
    <tbody id="formPrincipal:tablaTitulado_data" class="ui-datatable-data">
      {''.join(filas)}
    </tbody>
  </table>
</div>
"""
    return _doc(cuerpo, view_state)


def detalle(view_state: str, persona: dict, titulos: list) -> str:
    """
    Detalle del titulado: panelGrid de datos (sin inputs/botones) + una tabla
    'tablaAplicaciones' por categoría (precedida de su <h4>) + el botón del PDF.
    `titulos` = lista de {"categoria", "headers", "filas"}.
    """
    # Panel de datos personales (clave/valor). Debe contener "Identifica" y NO
    # tener inputs/botones (el parser lo exige para distinguirlo del formulario).
    filas_persona = "".join(
        f"<tr><td>{html.escape(str(k))}:</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in persona.items()
    )
    panel = f"""
<table class="ui-panelgrid ui-widget" id="formPrincipal:datosPersona">
  <tbody>
    {filas_persona}
  </tbody>
</table>
"""

    bloques = []
    for i, grupo in enumerate(titulos):
        cab = "".join(f"<th>{html.escape(h)}</th>" for h in grupo["headers"])
        cuerpo_filas = []
        for fila in grupo["filas"]:
            celdas = "".join(f"<td>{html.escape(str(c))}</td>" for c in fila)
            cuerpo_filas.append(f"<tr class='ui-widget-content'>{celdas}</tr>")
        bloques.append(f"""
<h4>{html.escape(grupo['categoria'])}</h4>
<div id="formPrincipal:j_idt50:{i}:tablaAplicaciones" class="ui-datatable ui-widget">
  <table>
    <thead><tr>{cab}</tr></thead>
    <tbody class="ui-datatable-data">
      {''.join(cuerpo_filas)}
    </tbody>
  </table>
</div>""")

    cuerpo = panel + "".join(bloques) + """
<div id="formPrincipal:gridBotonPdf">
  <button type="submit" id="formPrincipal:btnInfoConsulta"
          name="formPrincipal:btnInfoConsulta"
          value="formPrincipal:btnInfoConsulta">Imprimir Información</button>
</div>
"""
    return _doc(cuerpo, view_state)
