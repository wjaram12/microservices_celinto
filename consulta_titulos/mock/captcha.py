"""
Generación de imágenes de captcha para el servidor mock (Pillow).

Produce un PNG con texto alfanumérico sobre un fondo con ruido leve, parecido al
captcha real de SENESCYT, para que ddddocr lo resuelva de verdad. El ruido es
moderado a propósito: el OCR acierta la mayoría de las veces y, cuando falla, el
scraper pide un captcha nuevo y reintenta (se ejercita el bucle de reintento).

`generar()` devuelve (png_bytes, texto_esperado).
"""
import io
import random
import string

from PIL import Image, ImageDraw, ImageFont

# Sin caracteres ambiguos (0/O, 1/I/l) para que el OCR sea estable contra el mock.
ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
LARGO = 5

ANCHO, ALTO = 160, 60


def _fuente(tam=38):
    """Intenta una fuente TrueType común; cae a la fuente bitmap por defecto."""
    for nombre in ("arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            continue
    return ImageFont.load_default()


def generar():
    """Crea un captcha nuevo. Devuelve (png_bytes, texto)."""
    texto = "".join(random.choice(ALFABETO) for _ in range(LARGO))

    img = Image.new("RGB", (ANCHO, ALTO), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    fuente = _fuente()

    # Texto: cada carácter con un pequeño desplazamiento vertical.
    x = 12
    for ch in texto:
        y = random.randint(4, 14)
        draw.text((x, y), ch, font=fuente, fill=(0, 0, 0))
        x += 28

    # Ruido leve: algunas líneas y puntos.
    for _ in range(4):
        draw.line(
            [(random.randint(0, ANCHO), random.randint(0, ALTO)),
             (random.randint(0, ANCHO), random.randint(0, ALTO))],
            fill=(150, 150, 150), width=1)
    for _ in range(180):
        draw.point((random.randint(0, ANCHO), random.randint(0, ALTO)),
                   fill=(random.randint(120, 200),) * 3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), texto
