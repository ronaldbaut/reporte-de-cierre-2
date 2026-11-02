import asyncio
import discord
from discord.ext import commands
import os
import traceback
from openai import OpenAI
import json
import re
import unicodedata
from math import isfinite
from typing import Optional

print(">>> Iniciando bot de Reporte de Cierre...")

# ================== CONFIGURACIÓN ==================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("❌ ERROR: Falta la variable de entorno DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

# Estado por canal. Solo el usuario que inició puede responder.
# {
#   channel_id: {
#     "paso": int,
#     "user_id": int,
#     "answers": { "0": "...", ... },
#     "buffer": "",          # respuestas parciales de la pregunta actual
#     "sin_pedidos": bool,   # True si hoy no se alistaron pedidos
#     "gasoil_azul_lineas": float,  # líneas ya respondidas de la planta azul
#     "planta_azul_prendida": bool,  # True si dijeron que está prendida
#     "planta_azul_switch_confirmar": bool,  # esperando el sí de que lo apagaron
#   }
# }
conversation_state = {}

# Índices de preguntas del bloque de pedidos (0-based)
PEDIDOS_INICIO = 0
PEDIDOS_FIN = 4  # preguntas 1..5 inclusive

# Envasado de vasitos (0-based):
# 6 = horario, 7 = cantidad, 8 = número de personas, 9 = minutos de pausa.
IDX_VASITOS_HORAS = 6
IDX_VASITOS_CANTIDAD = 7
IDX_VASITOS_PERSONAS = 8
IDX_VASITOS_PAUSAS = 9

# Foto obligatoria de las cuatro batidoras, justo después de confirmar
# que se limpiaron los tambores (pregunta 10). El texto solo no alcanza.
# Vale una foto de las cuatro, o varias en el mismo mensaje. No mueve los
# índices de pedidos ni de vasitos.
IDX_BATIDORAS_FOTO = 12
TEXTO_BATIDORAS_FOTO = (
    "Envía una foto de las cuatro batidoras limpias. "
    "Si son varias, envíalas todas en el mismo mensaje."
)

# Planta azul (0-based), después de la foto de las batidoras.
# Prendida → líneas de antes de prenderla, y se saltan fotos, aceite y switch.
# Apagada → líneas, foto del nivel, foto de recarga si < 4, aceite, foto de
# aceite y, al final, el switch de la llave. La planta roja va justo después.
IDX_PLANTA_AZUL_PRENDIDA = 13
IDX_GASOIL_AZUL_LINEAS = 14
IDX_GASOIL_AZUL_FOTO_NIVEL = 15
IDX_GASOIL_AZUL_FOTO_RECARGA = 16
GASOIL_AZUL_MIN_LINEAS = 4

# Aceite planta azul, justo después del gasoil (la foto de recarga se salta
# si hay 4 líneas o más, y esta pregunta queda como la siguiente).
IDX_ACEITE_AZUL = IDX_GASOIL_AZUL_FOTO_RECARGA + 1
IDX_ACEITE_AZUL_FOTO = IDX_ACEITE_AZUL + 1
IDX_PLANTA_AZUL_SWITCH = IDX_ACEITE_AZUL_FOTO + 1

TEXTO_PLANTA_AZUL_PRENDIDA = "¿La planta azul está prendida?"
TEXTO_GASOIL_AZUL_LINEAS = "¿Cuántas líneas de gasoil tiene la planta azul?"
TEXTO_GASOIL_AZUL_LINEAS_ANTES = (
    "¿Cuántas líneas de gasoil tenía la planta azul antes de prenderla?"
)
TEXTO_PLANTA_AZUL_SWITCH = "¿Quedó el switch de la llave en apagado?"
TEXTO_APAGAR_SWITCH = "Apaga el switch de la llave de la planta azul."

# Meta CMI: segundos por vasito según cuántas personas envasaron
# 4 personas → 14 s; 3 → 18 s; 2 → 26 s. 5 o más usa la de 4.
METAS_SEGUNDOS_POR_VASITO = {
    2: 26,
    3: 18,
    4: 14,
}

# El bot solo menciona con <@id> a quien está respondiendo el cierre.
# Tania no tiene id acá, así que el aviso la nombra en texto.
MENSAJE_FALLO_META_ENVASADO = (
    "⚠️ **No pude calcular la meta de envasado de vasitos.** "
    "Tania, por favor revise este cierre."
)
MENSAJE_SIN_VASITOS = (
    "📊 **Envasado de vasitos:** No se envasaron vasitos hoy: no hay promedio que calcular."
)
NOTA_PAUSA_TANIA = (
    "• Nota: el descuento de estos minutos de pausa queda sujeto a la aprobación de Tania. "
    "Si no lo aprueba en sus observaciones, los extras semanales se recalculan "
    "con el tiempo bruto, sin ese descuento."
)

# Un intento extra si Grok falla, viene vacío o el JSON no trae horas usables.
ENVASADO_EXTRACCION_TIMEOUT = 30
ENVASADO_REINTENTO_ESPERA = 1

# ================== PREGUNTAS + REGLAS POR PREGUNTA ==================
# Cada item: texto que ve el trabajador + reglas que ve Grok.
PREGUNTAS = [
    {
        "texto": "1. ¿Qué trabajadores alistaron los pedidos?",
        "reglas": """
- Válido: nombres de 1+ trabajadores (ej. "Angel y Luis").
- Válido N/A: si dice que HOY NO se alistaron / no se hicieron / no hubo pedidos.
  En ese caso marca skip_bloque_pedidos=true.
- NO exijas nombres si ya dijeron que no hubo pedidos.
- Inválido: solo ".", vacío, "si", "ok", sin nombres y sin decir que no hubo pedidos.
""",
    },
    {
        "texto": "2. ¿A qué clientes pertenecen los pedidos que se alistaron?",
        "reglas": """
- Válido: lista de clientes/nombres.
- Válido N/A: no hubo / no se alistaron pedidos hoy.
- Si no hubo pedidos → skip_bloque_pedidos=true.
""",
    },
    {
        "texto": (
            "3. ¿Cómo quedaron los pedidos? (cuáles se completaron, cuáles quedaron "
            "pendientes, observaciones relevantes y cualquier ajuste para Producción mañana). "
            "En caso de que hayan pedidos incompletos, explica por qué quedaron incompletos."
        ),
        "reglas": """
- Acepta respuestas naturales. NO exijas copiar la pregunta entera.
- Válido si cubre la idea general: estado de pedidos (completos/incompletos) y, si hay faltantes, qué falta o por qué.
- "Los demás se completaron" + lista de faltantes por cliente ES válido.
- "Todos completos, sin ajustes" ES válido.
- N/A: no se alistaron pedidos hoy → skip_bloque_pedidos=true.
- NO rechaces solo porque no lista uno por uno los completados si ya listó los incompletos y dijo que el resto está completo.
- NO rechaces por falta de "ajustes para mañana" si dice "ningún ajuste" o equivalente.
""",
    },
    {
        "texto": "4. ¿En qué congeladores quedaron cuáles pedidos?",
        "reglas": """
- Válido: indica congelador(es) y, si hay varios, qué va en cada uno.
- Si solo hay un grupo y un congelador ("los dos en el gris"), ES válido.
- N/A: "en ninguno / no se alistaron" si no hubo pedidos → skip_bloque_pedidos=true.
- NO exijas re-listar todos los clientes si el contexto previo ya los tiene y la respuesta agrupa claro.
""",
    },
    {
        "texto": "5. ¿A qué hora se comenzaron a alistar los pedidos y a qué hora terminaron?",
        "reglas": """
- Válido: hora inicio y hora fin (cualquier formato: 8:50 PM, 20:50, de 7:33 a 7:48).
- N/A: no hubo pedidos / no hubo hora porque no se alistaron → skip_bloque_pedidos=true.
- NO exijas horas si ya dijeron que no hubo pedidos.
""",
    },
    {
        "texto": "6. ¿A qué hora empezaste y terminaste el envasado de bandejas? ¿Qué trabajadores se encargaron?",
        "reglas": """
- Válido si trae: hora inicio, hora fin (o tramos con pausas) Y nombres de quienes envasaron.
- Acepta respuestas en un solo mensaje o el buffer acumulado.
- Si falta SOLO una de las dos partes, pide solo lo que falta (no digas "responde completo").
""",
    },
    {
        "texto": (
            "7. ¿A qué hora empezaste y terminaste el envasado de vasitos? "
            "¿Qué trabajadores se encargaron del envasado de vasitos?"
        ),
        "reglas": """
- Igual que bandejas: horas + trabajadores.
- Acepta tramos (gourmet de X a Y, trisabor de A a B).
- Si no se envasaron vasitos hoy, "no hubo / no se envasaron" ES válido.
""",
    },
    {
        "texto": "8. ¿Cuántos vasitos se envasaron en total hoy?",
        "reglas": """
- Válido: un total numérico (729, 742 vasitos) O desglose que sume (Trisabor:415 Gourmet:434).
- NO exijas "total" si ya dio números claros de vasitos.
- Inválido: solo horas de envasado sin cantidad.
- "0" / "ninguno" / "no se envasaron vasitos" ES válido.
""",
    },
    {
        "texto": (
            "¿Cuántas personas envasaron los vasitos? "
            "Escribe el número. Si quieres, agrega los nombres después "
            "(ejemplo: 2, José Luis y Ángel)."
        ),
        "reglas": """
- Esta pregunta la resuelve el bot en código, no Grok.
- Válido solo con un número entero de personas (1 a 20).
- Nombres solos, sin número, no alcanzan: hay que volver a pedir el número.
- Ejemplo válido: "2, José Luis y Ángel".
""",
    },
    {
        "texto": (
            "¿Cuántos minutos pausaron durante el envasado de vasitos? "
            "Si no pausaron, escribe 0."
        ),
        "reglas": """
- Esta pregunta la resuelve el bot en código, no Grok.
- Válido: minutos con un número, de 0 a 480. 0 si no hubo pausa.
- "no", "ninguna" y "sin pausa" cuentan como 0.
""",
    },
    {
        "texto": "9. ¿A qué hora empezaste y terminaste la limpieza? ¿Qué trabajadores se encargaron?",
        "reglas": """
- Válido: hora inicio + hora fin + nombre(s).
- "Yuselis empezó 5:45pm hasta las 6:45pm" YA trae las tres cosas → válido.
- Si el buffer acumulado ya tiene horas y nombre, acepta aunque el último mensaje sea corto.
- NO entres en bucle pidiendo de nuevo lo que ya está en el buffer.
""",
    },
    {
        "texto": "10. ¿Limpiaste los tambores de las batidoras y retiraste la sal de los cabezotes?",
        "reglas": """
- Válido: confirma ambas cosas, o un "sí" claro a la pregunta completa.
- "Si están limpios y sin sal" ES válido.
- Si solo confirma una parte, pide solo la que falta.
""",
    },
    {
        "texto": TEXTO_BATIDORAS_FOTO,
        "reglas": """
- Esta pregunta la resuelve el bot en código, no Grok.
- Válido SOLO si hay una foto adjunta de las batidoras limpias.
- Una foto de las cuatro, o varias fotos en el mismo mensaje, son válidas.
- Sin adjunto es inválido, aunque haya texto (un "sí" no alcanza).
- Foto sola (sin texto) ES válida.
""",
    },
    {
        "texto": TEXTO_PLANTA_AZUL_PRENDIDA,
        "reglas": """
- Esta pregunta la resuelve el bot en código (sí/no), no Grok.
- Prendida: sí, si, s, yes, prendida, encendida, "está prendida".
- Apagada: no, n, apagada, "está apagada".
- Si no se entiende, se vuelve a preguntar.
""",
    },
    {
        "texto": TEXTO_GASOIL_AZUL_LINEAS,
        "reglas": """
- Válido SOLO si la respuesta es un número entero ≥ 0 (0, 1, 2, 3, 4, …).
- Inválido: texto, decimales, negativos, "medio tanque", "5 rayas", "cinco", "4 líneas".
- No pidas nivel de aceite: esta pregunta es solo el número de líneas.
- Si la planta estaba prendida, el bot pregunta en su lugar cuántas líneas tenía antes de prenderla. La validación es la misma.
""",
    },
    {
        "texto": "Envía una foto del nivel de gasoil de la planta azul.",
        "reglas": """
- Válido SOLO si hay una foto adjunta del nivel de gasoil.
- Sin adjunto es inválido, aunque haya texto.
- Foto sola (sin texto) ES válida.
""",
    },
    {
        "texto": "Envía una foto de la planta azul después de colocarle gasoil.",
        "reglas": """
- Válido SOLO si hay una foto adjunta después de recargar gasoil.
- Sin adjunto es inválido, aunque haya texto.
- Foto sola (sin texto) ES válida.
- Esta pregunta solo se hace si las líneas de gasoil eran menos de 4.
""",
    },
    {
        "texto": "11. ¿Cuál es el nivel de aceite de la planta azul?",
        "reglas": """
- Válido: algo del aceite (bien / óptimo / bajo / foto).
- "aceite bien" ES válido.
- "óptimo", "bajo" y "bien" ES válido.
- Si la planta está en uso y no puede medir, acepta esa explicación + lo que sísepa.
- Foto + texto corto sobre niveles ES válido.
- No pidas gasoil: esta pregunta es solo el nivel de aceite de la planta azul.
""",
    },
    {
        "texto": "Envía una foto del nivel de aceite de la planta azul.",
        "reglas": """
- Válido SOLO si hay una foto adjunta del nivel de aceite.
- Sin adjunto es inválido, aunque haya texto.
- Foto sola (sin texto) ES válida.
""",
    },
    {
        "texto": TEXTO_PLANTA_AZUL_SWITCH,
        "reglas": """
- Esta pregunta la resuelve el bot en código (sí/no), no Grok.
- Sí: el switch quedó apagado.
- No: hay que apagarlo y confirmar con sí.
- Si no se entiende, se vuelve a preguntar.
""",
    },
    {
        "texto": "12. ¿Cuánto gasoil tiene ahora la planta roja y cuál es el nivel de aceite?",
        "reglas": """
- Válido: cantidad o nivel de gasoil (rayas, líneas, mitad, pimpina, número) Y algo del aceite (bien / óptimo / bajo / foto).
- "Medio tanque de gasolina y aceite bien" ES válido.
- "A la mitad del tanque y aceite bien" ES válido.
- NO digas "no me dijiste cuánto gasoil" si ya dijo mitad / medio tanque / rayas.
- NO confundas esta pregunta con la de congeladores ni luces.
""",
    },
    {
        "texto": "13. ¿Todos los motores de los congeladores están encendidos, bien cerrados y calientes?",
        "reglas": """
- "Sí" o "Si" ES válido (la pregunta es de sí/no).
- "Todos funcionando, encendidos y calientes" ES válido.
- NO obligues a copiar la pregunta completa.
""",
    },
    {
        "texto": (
            "14. ¿Cerraste con candado las puertas de los heladeros, el estacionamiento, "
            "la planta y la salida (con doble candado)?"
        ),
        "reglas": """
- "Sí" / "Si" / "todas con candado" ES válido.
- Si detalla puertas, mejor, pero no es obligatorio si dice sí a la pregunta.
""",
    },
    {
        "texto": "15. ¿Quedó apagado el aire acondicionado de producción y el de ventas?",
        "reglas": """
- "Sí", "Si", "apagados los dos" ES válido.
""",
    },
    {
        "texto": (
            "16. ¿Quedó registrada la producción del día completa en la hoja de cálculo "
            "correspondiente? ¿Quién lo hizo?"
        ),
        "reglas": """
- Válido: sí/no + nombre de quien registró (si fue sí).
- "Si, lo hizo Luis" ES válido.
""",
    },
    {
        "texto": "17. ¿Oprimiste el botón rojo del tablero de batidoras?",
        "reglas": """
- "Sí" / "Si" ES válido.
""",
    },
    {
        "texto": "18. ¿Dejaron todas las luces de la fábrica apagadas?",
        "reglas": """
- "Sí" / "Si" / "todas apagadas" ES válido.
- NO digas "responde completo" ante un "Sí" claro.
""",
    },
    {
        "texto": "19. ¿Qué cantidad de gasoil hay en el depósito?",
        "reglas": """
- Válido: conteo de pimpinas/envases (ej. "4 de 30L y 8,5 de 25L" o "10 de 25 y 4 de 30").
- Eso YA es la cantidad del depósito. NO exijas "total en litros" ni "cantidad total" aparte.
- "4 de 30 y 5 de 25" ES válido.
""",
    },
    {
        "texto": "20. ¿Recibiste todos los implementos de ventas de San Cristóbal?",
        "reglas": """
- "Sí" / "Si" / "No" ES válido (la pregunta es de sí/no).
- "No" también es válido: el detalle de qué faltó o sobró va en la pregunta siguiente.
- NO obligues a copiar la pregunta.
""",
    },
    {
        "texto": "21. ¿Qué faltó o sobró de: bolsas de papel, bolsas de plástico, bolsos grandes, bolsos pequeños y pilas?",
        "reglas": """
- Válido: respuestas con "faltó" seguido de lo que faltó y/o "sobró" seguido de lo que sobró, de todos los artículos o solo de algunos.
- Ejemplos válidos: "faltó 2 pilas", "sobró 10 bolsas de papel", "faltó 1 bolso grande, sobró 5 bolsas de plástico".
- Los artículos no mencionados se entienden completos: NO exijas listar todos los artículos.
- "nada, llegó todo completo" / "no faltó ni sobró nada" sigue siendo válido.
- También válido: "no faltó nada" y/o "no sobró nada", solos o combinados con la otra parte (ej. "no faltó nada, sobró 3 pilas").
- Inválido: vacío, ".", "ok", o solo "sí"/"si" sin información.
- NO obligues a copiar la pregunta.
""",
    },
    {
        "texto": "22. ¿Enviaste el mensaje de lo que llegó?",
        "reglas": """
- "Sí" / "Si" / "No" ES válido (la pregunta es de sí/no).
- "No" también es válido.
- NO obligues a copiar la pregunta.
""",
    },
]


# ================== HELPERS ==================
def _vacio_o_basura(texto: str, tiene_adjuntos: bool) -> bool:
    t = (texto or "").strip()
    if tiene_adjuntos and not t:
        return False  # foto/video solo puede ser válido en algunas preguntas
    if not t:
        return True
    basura = {".", "..", "...", "ok", "vale", "listo", "k", "ya", "-", "—"}
    return t.lower() in basura


def _parece_cancelar(content_lower: str) -> bool:
    # Solo frases de cancelación, no cualquier palabra suelta en un párrafo largo
    patrones = [
        r"^cancelar(\s+reporte)?$",
        r"^cancel(\s+report)?$",
        r"^cancela(r)?$",
        r"^canelar(\s+reporte)?$",
        r"^reporte\s+cancelado$",
        r"^detener(\s+reporte)?$",
    ]
    return any(re.match(p, content_lower) for p in patrones)


def _parece_inicio(content_lower: str) -> bool:
    return content_lower in {
        "reporte de cierre",
        "iniciar reporte de cierre",
        "inicio reporte de cierre",
        "/reporte-cierre",
    }


def _saltar_resto_pedidos(state: dict) -> None:
    """Si no hubo pedidos, salta a la pregunta 6 (índice 5)."""
    state["sin_pedidos"] = True
    if state["paso"] <= PEDIDOS_FIN:
        state["paso"] = PEDIDOS_FIN + 1
        state["buffer"] = ""


def _lineas_gasoil(texto: str) -> Optional[float]:
    """Líneas de gasoil ≥ 0, o None si el mensaje no es solo ese número.

    Acepta entero o decimal con coma o punto ("4", "4,5", "4.5") y, al final,
    la palabra línea o líneas (mayúsculas y acentos no importan).
    """
    t = (texto or "").strip().lower()
    if not t:
        return None
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?: lineas?)?", t)
    if not m:
        return None
    try:
        valor = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if not isfinite(valor) or valor < 0:
        return None
    return valor


# Sí/no de la planta azul. Las palabras de estado cambian de sentido según
# la pregunta: "apagada" niega "¿está prendida?" y afirma "¿quedó apagado?".
_SI_EXACTOS = {"si", "s", "yes", "sip", "sep", "afirmativo"}
_NO_EXACTOS = {"no", "n", "nop", "nope", "negativo"}
_RELLENO_SI_NO = r"(?:ok|vale|bueno|pues|claro|ya|gracias)"
_PALABRAS_PRENDIDA = (
    "prendida",
    "prendido",
    "encendida",
    "encendido",
    "prendio",
    "encendio",
)
_PALABRAS_APAGADA = ("apagada", "apagado", "apago", "apague", "apagaron")


def _normalizar_texto_respuesta(texto: str) -> str:
    """Minúsculas, sin acentos. Punto, salto y punto y coma separan cláusulas."""
    t = (texto or "").strip().lower()
    if not t:
        return ""
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("¿", " ").replace("¡", " ")
    t = re.sub(r"[!?]+", " ", t)
    t = re.sub(r"[\n\r.;]+", ",", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"(?:,\s*)+", ", ", t)
    return t.strip(" ,")


def _es_duda(texto: str) -> bool:
    if re.search(
        r"\b(ni idea|quizas|tal vez|mas o menos|puede ser|no estoy segur[oa]|depende|nose)\b",
        texto,
    ):
        return True
    if re.fullmatch(r"no se( si)?", texto) or re.fullmatch(r"no lo se( si)?", texto):
        return True
    if re.search(r"\bno se si\b", texto) or re.search(r"\bno lo se\b", texto):
        return True
    return False


def _si_no_puro(texto: str) -> Optional[bool]:
    """Sí/no solo, o con relleno corto ('claro si', 'no ya'). None si hay más."""
    if texto in _SI_EXACTOS:
        return True
    if texto in _NO_EXACTOS:
        return False
    si = r"(?:si|s|yes|sip|sep|afirmativo)"
    no = r"(?:no|n|nop|nope|negativo)"
    relleno = _RELLENO_SI_NO
    if re.fullmatch(rf"{relleno}(?:\s+{relleno})*(?:\s+que)?\s+{si}", texto):
        return True
    if re.fullmatch(rf"{relleno}(?:\s+{relleno})*(?:\s+que)?\s+{no}", texto):
        return False
    if re.fullmatch(rf"{si}(?:\s+{relleno})+", texto):
        return True
    if re.fullmatch(rf"{no}(?:\s+{relleno})+", texto):
        return False
    return None


def _tiene_palabra(texto: str, palabras: tuple) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", texto) for p in palabras)


def _palabra_negada(texto: str, palabra: str) -> bool:
    """'no está prendida' / 'no se apagó', no un 'no' suelto de otra cláusula."""
    return (
        re.search(
            rf"\bno\b(?:\s+\w+){{0,6}}\s+{re.escape(palabra)}\b",
            texto,
        )
        is not None
    )


def _clasificar_clausula(texto: str, afirmacion: tuple, negacion: tuple) -> Optional[bool]:
    t = texto.strip()
    if not t or _es_duda(t):
        return None
    tiene_af = _tiene_palabra(t, afirmacion)
    tiene_ng = _tiene_palabra(t, negacion)
    puro = _si_no_puro(t)
    if not tiene_af and not tiene_ng:
        return puro
    if tiene_af and tiene_ng:
        return None
    if re.search(r"\bsi\b", t) and re.search(r"\bno\b", t):
        return None
    palabras = afirmacion if tiene_af else negacion
    presentes = [p for p in palabras if re.search(rf"\b{re.escape(p)}\b", t)]
    negada = any(_palabra_negada(t, p) for p in presentes)
    base = True if tiene_af else False
    return (not base) if negada else base


def _clasificar_si_no(texto: str, afirmacion: tuple, negacion: tuple) -> Optional[bool]:
    """True/False si se entiende; None para volver a preguntar."""
    t = _normalizar_texto_respuesta(texto)
    if not t or _es_duda(t):
        return None
    partes = [p.strip() for p in t.split(",") if p.strip()]
    if len(partes) <= 1:
        return _clasificar_clausula(t, afirmacion, negacion)

    estados = []
    puros = []
    for parte in partes:
        if _es_duda(parte):
            continue
        valor = _clasificar_clausula(parte, afirmacion, negacion)
        if valor is None:
            continue
        if _tiene_palabra(parte, afirmacion) or _tiene_palabra(parte, negacion):
            estados.append(valor)
        else:
            puros.append(valor)
    if estados:
        if not all(v == estados[0] for v in estados):
            return None
        if puros and not all(v == estados[0] for v in puros):
            return None
        return estados[0]
    if puros and all(v == puros[0] for v in puros):
        return puros[0]
    return None


def _planta_azul_esta_prendida(texto: str) -> Optional[bool]:
    return _clasificar_si_no(texto, _PALABRAS_PRENDIDA, _PALABRAS_APAGADA)


def _switch_quedo_apagado(texto: str) -> Optional[bool]:
    """True si el switch quedó apagado. 'apagado' aquí es un sí."""
    return _clasificar_si_no(texto, _PALABRAS_APAGADA, _PALABRAS_PRENDIDA)


def _texto_lineas_gasoil_azul(state: dict) -> str:
    if state.get("planta_azul_prendida"):
        return TEXTO_GASOIL_AZUL_LINEAS_ANTES
    return TEXTO_GASOIL_AZUL_LINEAS


def _respuesta_con_adjuntos(texto: str, nombres_adjuntos) -> str:
    """Mismo formato que ya se arma para la validación: texto + [Adjuntos: …]."""
    cuerpo = (texto or "").strip()
    if not nombres_adjuntos:
        return cuerpo
    nota = "[Adjuntos: " + ", ".join(nombres_adjuntos) + "]"
    if cuerpo:
        return f"{cuerpo}\n{nota}"
    return nota


def _siguiente_tras_foto_nivel_gasoil(lineas: float):
    """Si hay menos de 4 líneas, toca la foto post-recarga. Si no, la pregunta siguiente."""
    if lineas < GASOIL_AZUL_MIN_LINEAS:
        return IDX_GASOIL_AZUL_FOTO_RECARGA, True
    return IDX_GASOIL_AZUL_FOTO_RECARGA + 1, False


async def _preguntar_o_cerrar(message: discord.Message, state: dict) -> None:
    if state["paso"] < len(PREGUNTAS):
        await message.channel.send(PREGUNTAS[state["paso"]]["texto"])
        return
    channel_id = str(message.channel.id)
    await message.channel.send(
        "✅ **Reporte de Cierre completado.** ¡Gracias!\n"
        f"_Respondido por <@{state['user_id']}>._"
    )
    print(
        f"[CIERRE OK] channel={channel_id} "
        f"answers={json.dumps(state['answers'], ensure_ascii=False)} "
        f"calculo={json.dumps(state.get('calculo_envasado'), ensure_ascii=False)}"
    )
    del conversation_state[channel_id]


def _mensaje_confirmar_switch() -> str:
    return f"{TEXTO_APAGAR_SWITCH}\nCuando lo hayas apagado, responde sí."


async def _guardar_foto_obligatoria(
    message: discord.Message,
    state: dict,
    texto: str,
    tiene_adjuntos: bool,
) -> bool:
    """True si había foto y quedó guardada. Si no, pide la foto y no avanza.

    Igual para las batidoras y para las fotos de la planta azul: vale una
    imagen o varias en el mismo mensaje. El texto solo no alcanza. Los
    nombres quedan en la respuesta como [Adjuntos: …], que es lo que se
    imprime al cerrar el reporte.
    """
    paso = state["paso"]
    if not tiene_adjuntos:
        if texto and not _vacio_o_basura(texto, False):
            if state["buffer"]:
                state["buffer"] = (state["buffer"] + "\n" + texto).strip()
            else:
                state["buffer"] = texto
        await message.channel.send(
            "Falta la foto. Adjunta la imagen en el mensaje.\n\n"
            f"{PREGUNTAS[paso]['texto']}"
        )
        return False

    try:
        await message.add_reaction("✅")
    except Exception:
        pass

    if state["buffer"]:
        buffer_completo = (state["buffer"] + "\n" + texto).strip()
    else:
        buffer_completo = texto
    nombres = [a.filename for a in message.attachments]
    state["answers"][str(paso)] = _respuesta_con_adjuntos(buffer_completo, nombres)
    state["buffer"] = ""
    return True


async def _manejar_foto_batidoras(
    message: discord.Message,
    state: dict,
    texto: str,
    tiene_adjuntos: bool,
) -> None:
    if not await _guardar_foto_obligatoria(message, state, texto, tiene_adjuntos):
        return
    state["paso"] += 1
    await _preguntar_o_cerrar(message, state)


async def _manejar_gasoil_planta_azul(
    message: discord.Message,
    state: dict,
    texto: str,
    tiene_adjuntos: bool,
) -> None:
    paso = state["paso"]

    if paso == IDX_PLANTA_AZUL_PRENDIDA:
        prendida = _planta_azul_esta_prendida(texto)
        if prendida is None:
            await message.channel.send(
                "No entendí. Responde si está prendida o apagada "
                "(por ejemplo: sí, prendida, no, apagada).\n\n"
                f"{PREGUNTAS[IDX_PLANTA_AZUL_PRENDIDA]['texto']}"
            )
            return
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        state["answers"][str(paso)] = texto
        state["planta_azul_prendida"] = prendida
        state["buffer"] = ""
        state["paso"] = IDX_GASOIL_AZUL_LINEAS
        await message.channel.send(_texto_lineas_gasoil_azul(state))
        return

    if paso == IDX_GASOIL_AZUL_LINEAS:
        n = _lineas_gasoil(texto)
        if n is None:
            if texto and not _vacio_o_basura(texto, False):
                if state["buffer"]:
                    state["buffer"] = (state["buffer"] + "\n" + texto).strip()
                else:
                    state["buffer"] = texto
            await message.channel.send(
                "Tiene que ser un número, 0 o más (ej. 4 o 4,5).\n\n"
                f"{_texto_lineas_gasoil_azul(state)}"
            )
            return
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        state["answers"][str(paso)] = str(int(n)) if n.is_integer() else str(n)
        state["gasoil_azul_lineas"] = n
        state["buffer"] = ""
        if state.get("planta_azul_prendida"):
            # Mismo criterio que la foto de recarga: el paso que no se hace
            # no se guarda en answers.
            state["paso"] = IDX_PLANTA_AZUL_SWITCH + 1
            await _preguntar_o_cerrar(message, state)
            return
        state["paso"] = IDX_GASOIL_AZUL_FOTO_NIVEL
        await message.channel.send(PREGUNTAS[IDX_GASOIL_AZUL_FOTO_NIVEL]["texto"])
        return

    if paso == IDX_PLANTA_AZUL_SWITCH:
        if state.get("planta_azul_switch_confirmar"):
            if _switch_quedo_apagado(texto) is not True:
                await message.channel.send(_mensaje_confirmar_switch())
                return
            previo = (state.get("buffer") or "").strip()
            state["answers"][str(paso)] = f"{previo}\n{texto}".strip() if previo else texto
            state["buffer"] = ""
            state["planta_azul_switch_confirmar"] = False
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            state["paso"] = paso + 1
            await _preguntar_o_cerrar(message, state)
            return

        quedo_apagado = _switch_quedo_apagado(texto)
        if quedo_apagado is None:
            await message.channel.send(
                "No entendí. Responde sí o no.\n\n"
                f"{PREGUNTAS[IDX_PLANTA_AZUL_SWITCH]['texto']}"
            )
            return
        if not quedo_apagado:
            state["buffer"] = texto
            state["planta_azul_switch_confirmar"] = True
            await message.channel.send(_mensaje_confirmar_switch())
            return
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        state["answers"][str(paso)] = texto
        state["buffer"] = ""
        state["paso"] = paso + 1
        await _preguntar_o_cerrar(message, state)
        return

    if not await _guardar_foto_obligatoria(message, state, texto, tiene_adjuntos):
        return

    if paso == IDX_GASOIL_AZUL_FOTO_NIVEL:
        lineas = state.get("gasoil_azul_lineas")
        if lineas is None:
            lineas = _lineas_gasoil(
                state["answers"].get(str(IDX_GASOIL_AZUL_LINEAS), "")
            )
        if lineas is None:
            lineas = 0
        siguiente, avisar = _siguiente_tras_foto_nivel_gasoil(lineas)
        state["paso"] = siguiente
        if avisar:
            await message.channel.send("Debes colocarle gasoil a la planta azul.")
        await _preguntar_o_cerrar(message, state)
        return

    state["paso"] += 1
    await _preguntar_o_cerrar(message, state)


# ================== CÁLCULO ENVASADO VASITOS ==================
def _a_minutos(hora: str) -> Optional[int]:
    """Convierte '15:20', '3:20pm', '3.20 p.m.' a minutos desde medianoche."""
    if not hora or not str(hora).strip():
        return None
    s = str(hora).strip().lower()
    s = s.replace(" ", "")
    s = s.replace("p.m.", "pm").replace("a.m.", "am")
    s = s.replace("p.m", "pm").replace("a.m", "am")
    mer = None
    if s.endswith("pm"):
        mer = "pm"
        s = s[:-2]
    elif s.endswith("am"):
        mer = "am"
        s = s[:-2]
    s = s.replace(".", ":").replace("h", ":")
    parts = [p for p in s.split(":") if p != ""]
    if not parts:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    if mer == "pm" and h < 12:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    elif mer is None and 1 <= h <= 7:
        # Envasado es turno tarde: 3:20 → 15:20
        h += 12
    return h * 60 + m


def _duracion_minutos(inicio: str, fin: str) -> Optional[int]:
    t0 = _a_minutos(inicio)
    t1 = _a_minutos(fin)
    if t0 is None or t1 is None:
        return None
    if t1 <= t0:
        t1 += 12 * 60
        if t1 <= t0:
            t1 += 12 * 60
    return t1 - t0


def _fmt_duracion(minutos: float) -> str:
    total = max(0, int(round(minutos)))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def _fmt_segundos_vasito(valor: float) -> str:
    if abs(valor - round(valor)) < 0.05:
        return str(int(round(valor)))
    return f"{valor:.1f}".replace(".", ",")


def _meta_para_personas(n: int) -> Optional[int]:
    if n >= 4:
        return METAS_SEGUNDOS_POR_VASITO[4]
    return METAS_SEGUNDOS_POR_VASITO.get(n)


def _vasitos_entero_simple(texto: str) -> Optional[int]:
    """Cantidad si la respuesta es solo un entero ('673', '742 vasitos')."""
    t = _normalizar_texto_respuesta(texto)
    m = re.fullmatch(r"(\d+)\s*(vasitos?|unidades?)?", t)
    if not m:
        return None
    return int(m.group(1))


def _respuesta_sin_vasitos(texto: str) -> bool:
    """True si dijeron que hoy no se envasaron vasitos."""
    t = _normalizar_texto_respuesta(texto)
    if not t:
        return False
    if t in {
        "0",
        "cero",
        "nada",
        "ninguno",
        "ningunos",
        "ninguna",
        "no",
        "no hubo",
        "no se envasaron",
        "no se envaso",
        "no envasamos",
        "no hubo vasitos",
        "sin vasitos",
        "0 vasitos",
        "cero vasitos",
        "ningun vasito",
        "ninguno vasito",
    }:
        return True
    if re.fullmatch(r"0+\s*(vasitos?|unidades?)?", t):
        return True
    if re.fullmatch(r"(cero|ninguno|ningunos|nada)\s*(vasitos?)?", t):
        return True
    if re.search(r"\b(no|sin|ningun|ninguno)\b", t) and re.search(
        r"\b(vasitos?|envasado|envasaron|envaso|envasar)\b", t
    ):
        if re.search(r"[1-9]", t):
            return False
        return True
    nums = re.findall(r"\d+", t)
    if nums and all(int(n) == 0 for n in nums) and not re.search(r"[1-9]", t):
        return True
    return False


def _numero_personas(texto: str) -> Optional[int]:
    """Entero explícito de personas. None si hay que adivinar por los nombres.

    «José Luis y ángel» no trae número: puede ser 2 o 3, y no se cuenta.
    «2, José Luis y Ángel» sí: el 2 es la cantidad que vale para la meta.
    """
    t = _normalizar_texto_respuesta(texto)
    if not t:
        return None
    nums = re.findall(r"\d+", t)
    if len(set(nums)) != 1:
        return None
    n = int(nums[0])
    if not 1 <= n <= 20:
        return None
    return n


_RE_RELLENO_PERSONAS = re.compile(
    r"\b(personas?|trabajadores?|trabajador|fueron|fuimos|somos|son|éramos|eramos|nombres?)\b",
    re.IGNORECASE,
)


def _detalle_personas(texto: str) -> str:
    """Nombres que acompañan al número, para mostrarlos. No se usan para contar."""
    t = _RE_RELLENO_PERSONAS.sub(" ", texto or "")
    t = re.sub(r"\d+", " ", t)
    t = t.replace("\n", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip(" ,.;:-–—")


_PAUSA_CERO_EXACTA = {
    "0",
    "cero",
    "nada",
    "ninguna",
    "ninguno",
    "no",
    "nop",
    "sin pausa",
    "sin pausas",
    "no pausamos",
    "no paramos",
    "no hubo",
    "no hubo pausa",
    "no hubo pausas",
}


def _minutos_pausa(texto: str) -> Optional[int]:
    """Minutos de pausa, 0 a 480. None si no hay un número claro."""
    t = _normalizar_texto_respuesta(texto)
    if not t:
        return None
    if t in _PAUSA_CERO_EXACTA:
        return 0
    if re.fullmatch(r"(no|sin|ninguna?)\s+(hubo\s+)?(pausa|pausas|paramos|pausamos)", t):
        return 0
    # «1 hora» no se convierte solo: la pregunta pide minutos.
    if re.search(r"\b(vasitos?|personas?|horas?)\b", t):
        return None
    nums = re.findall(r"\d+", t)
    if len(set(nums)) != 1:
        return None
    n = int(nums[0])
    if not 0 <= n <= 480:
        return None
    return n


def resolver_personas_envasado(buffer: str, texto: str) -> dict:
    """Junta el buffer de la pregunta con el mensaje nuevo. No avanza si no hay número."""
    if _vacio_o_basura(texto, False):
        return {"ok": False, "vacio": True, "buffer": buffer or ""}
    texto = texto.strip()
    previo = (buffer or "").strip()
    completo = f"{previo}\n{texto}".strip() if previo else texto
    n = _numero_personas(completo)
    respuesta = completo
    if n is None:
        n_ultimo = _numero_personas(texto)
        if n_ultimo is not None:
            n = n_ultimo
            respuesta = texto
    if n is None:
        return {"ok": False, "vacio": False, "buffer": completo}
    return {
        "ok": True,
        "n": n,
        "detalle": _detalle_personas(respuesta),
        "respuesta": respuesta,
        "buffer": "",
    }


def resolver_pausas_envasado(buffer: str, texto: str) -> dict:
    if _vacio_o_basura(texto, False):
        return {"ok": False, "vacio": True, "buffer": buffer or ""}
    texto = texto.strip()
    previo = (buffer or "").strip()
    completo = f"{previo}\n{texto}".strip() if previo else texto
    pausa = _minutos_pausa(completo)
    respuesta = completo
    if pausa is None:
        pausa_ultima = _minutos_pausa(texto)
        if pausa_ultima is not None:
            pausa = pausa_ultima
            respuesta = texto
    if pausa is None:
        return {"ok": False, "vacio": False, "buffer": completo}
    return {"ok": True, "pausa": pausa, "respuesta": respuesta, "buffer": ""}


def _tiene_tramo_util(datos: dict) -> bool:
    for tramo in datos.get("tramos") or []:
        if not isinstance(tramo, dict):
            continue
        if _duracion_minutos(tramo.get("inicio") or "", tramo.get("fin") or "") is not None:
            return True
    return False


def _minutos_brutos_tramos(datos: dict) -> float:
    """Suma de reloj de los tramos, sin restar pausas."""
    total = 0.0
    hubo = False
    for tramo in datos.get("tramos") or []:
        if not isinstance(tramo, dict):
            continue
        dur = _duracion_minutos(tramo.get("inicio") or "", tramo.get("fin") or "")
        if dur is None:
            continue
        hubo = True
        total += dur
    if hubo:
        return float(total)
    try:
        return float(datos.get("minutos_totales") or 0)
    except (TypeError, ValueError):
        return 0.0


def _tiene_tiempo_util(datos: dict) -> bool:
    if _tiene_tramo_util(datos):
        return True
    try:
        return float(datos.get("minutos_totales") or 0) > 0
    except (TypeError, ValueError):
        return False


def _extraccion_envasado_completa(data: dict, q8: str = "") -> bool:
    """True si el JSON ya alcanza para decidir, y no hace falta reintentar."""
    if not isinstance(data, dict) or not data:
        return False
    if _respuesta_sin_vasitos(q8):
        return True
    q8_simple = _vasitos_entero_simple(q8)
    if q8_simple is not None and q8_simple <= 0:
        return True
    if q8_simple is not None and q8_simple > 0:
        return _tiene_tiempo_util(data)
    if data.get("hubo_envasado") is False and not _tiene_tiempo_util(data):
        return False
    try:
        vasitos = float(str(data.get("vasitos")).replace(",", "."))
    except (TypeError, ValueError):
        return False
    if vasitos <= 0:
        return data.get("hubo_envasado") is False
    return _tiene_tiempo_util(data)


def _mensaje_fallo_meta_envasado(razon: str = "") -> str:
    razon = (razon or "").strip()
    if razon:
        return f"{MENSAJE_FALLO_META_ENVASADO}\n_{razon}_"
    return MENSAJE_FALLO_META_ENVASADO


def calcular_envasado_vasitos(
    datos: dict,
    n_personas: Optional[int] = None,
    pausa_minutos: Optional[float] = None,
    personas_detalle: str = "",
) -> Optional[dict]:
    """
    Horas y vasitos vienen de la extracción. El número de personas y la pausa
    vienen de las preguntas explícitas: no se infieren de los nombres ni de
    pausa_minutos que haya armado el modelo dentro de un tramo.

    El promedio es tiempo neto / vasitos (no minutos-persona).
    Tiempo neto = tiempo bruto de reloj − minutos de pausa.
    """
    if not isinstance(datos, dict) or not datos:
        return {
            "ok": False,
            "motivo": "extraccion_vacia",
            "mensaje": "No se pudieron interpretar las horas ni la cantidad de vasitos.",
        }

    if datos.get("hubo_envasado") is False:
        return {
            "ok": False,
            "motivo": "no_vasitos",
            "mensaje": "No se envasaron vasitos hoy: no hay promedio que calcular.",
        }

    try:
        vasitos = int(round(float(str(datos.get("vasitos", 0)).replace(",", "."))))
    except (TypeError, ValueError):
        vasitos = 0
    if vasitos <= 0:
        return {
            "ok": False,
            "motivo": "no_vasitos",
            "mensaje": "No se envasaron vasitos hoy: no hay promedio que calcular.",
        }

    try:
        n = int(n_personas) if n_personas is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return {
            "ok": False,
            "motivo": "sin_personas",
            "mensaje": "No quedó el número de personas que envasaron.",
        }

    if pausa_minutos is None:
        return {
            "ok": False,
            "motivo": "sin_pausa",
            "mensaje": "No quedaron los minutos de pausa del envasado.",
        }
    try:
        pausa = float(pausa_minutos)
    except (TypeError, ValueError):
        pausa = -1
    if not isfinite(pausa) or pausa < 0:
        return {
            "ok": False,
            "motivo": "sin_pausa",
            "mensaje": "No quedaron los minutos de pausa del envasado.",
        }

    minutos_brutos = _minutos_brutos_tramos(datos)
    if minutos_brutos <= 0:
        return {
            "ok": False,
            "motivo": "sin_tiempo",
            "mensaje": (
                "Tengo el total de vasitos, pero no pude armar el tiempo de envasado "
                "con las horas. El promedio no se calculó."
            ),
        }

    neto = minutos_brutos - pausa
    if neto <= 0:
        return {
            "ok": False,
            "motivo": "pausa_cubre_tiempo",
            "mensaje": (
                f"La pausa indicada ({_fmt_duracion(pausa)}) cubre todo el tiempo "
                f"de envasado ({_fmt_duracion(minutos_brutos)}), así que no queda "
                "tiempo neto para el promedio."
            ),
            "minutos_brutos": minutos_brutos,
            "pausa_minutos": pausa,
        }

    segundos = neto * 60.0
    seg_por_vasito = segundos / vasitos
    if not isfinite(seg_por_vasito) or seg_por_vasito <= 0:
        return {
            "ok": False,
            "motivo": "calculo",
            "mensaje": "No pude calcular el promedio de segundos por vasito.",
        }

    meta = _meta_para_personas(n)
    cumplio = None
    if meta is not None:
        cumplio = seg_por_vasito <= meta + 1e-9

    detalle = (personas_detalle or "").strip()
    pausa_out = int(pausa) if float(pausa).is_integer() else pausa
    return {
        "ok": True,
        "vasitos": vasitos,
        "n_trabajadores": n,
        "personas_detalle": detalle,
        "trabajadores": [detalle] if detalle else [],
        "minutos_brutos": minutos_brutos,
        "pausa_minutos": pausa_out,
        "minutos": neto,
        "segundos": segundos,
        "seg_por_vasito": seg_por_vasito,
        "meta": meta,
        "cumplio": cumplio,
    }


def formatear_calculo_envasado(calc: dict) -> str:
    if not calc or not calc.get("ok"):
        if calc and calc.get("motivo") == "no_vasitos":
            return f"📊 **Envasado de vasitos:** {calc.get('mensaje')}"
        return _mensaje_fallo_meta_envasado((calc or {}).get("mensaje") or "")

    n = calc.get("n_trabajadores") or 0
    detalle = (calc.get("personas_detalle") or "").strip()
    if not detalle:
        nombres = [str(x).strip() for x in (calc.get("trabajadores") or []) if str(x).strip()]
        detalle = ", ".join(nombres)
    quienes = f" ({detalle})" if detalle else ""
    personas = f"{n} persona" + ("" if n == 1 else "s") + quienes
    bruto = calc.get("minutos_brutos", calc.get("minutos"))
    pausa = calc.get("pausa_minutos") or 0

    lineas = [
        "📊 **Cálculo de envasado de vasitos**",
        f"• Tiempo bruto: {_fmt_duracion(bruto)} ({int(round(float(bruto) * 60))} s)",
        f"• **Pausa descontada: {_fmt_duracion(pausa)}**",
        f"• Tiempo neto: {_fmt_duracion(calc['minutos'])} ({int(round(calc['segundos']))} s)",
        f"• Vasitos: {calc['vasitos']}",
        f"• Personas: {personas}",
        f"• Promedio: **{_fmt_segundos_vasito(calc['seg_por_vasito'])} s/vasito**",
    ]
    meta = calc.get("meta")
    if meta is None:
        if n <= 0:
            lineas.append("• Meta: no pude saber cuántas personas envasaron, no comparo contra el CMI.")
        else:
            lineas.append(
                f"• Meta: el CMI no define segundos/vasito para {n} persona(s) "
                f"(sí para 2, 3 o 4)."
            )
    else:
        etiqueta_n = "4 o más" if n >= 4 else str(n)
        lineas.append(f"• Meta ({etiqueta_n} personas): **{meta} s/vasito**")
        diff = meta - calc["seg_por_vasito"]
        if calc.get("cumplio"):
            lineas.append(
                f"✅ **Cumplieron la meta** "
                f"({_fmt_segundos_vasito(diff)} s por debajo)."
            )
        else:
            lineas.append(
                f"❌ **No cumplieron la meta** "
                f"({_fmt_segundos_vasito(-diff)} s por encima)."
            )
    if float(pausa) > 0:
        lineas.append(NOTA_PAUSA_TANIA)
    return "\n".join(lineas)


_PROMPT_EXTRACCION_ENVASADO = """
Eres extractor de datos de un reporte de cierre (fábrica de helados).
NO calcules promedios ni metas. Solo extrae lo que dijeron.

Responde ÚNICAMENTE JSON:
{
  "hubo_envasado": true o false,
  "vasitos": número entero (0 si no hubo),
  "n_trabajadores": null,
  "trabajadores": [],
  "tramos": [
    {
      "etiqueta": "gourmet" o "trisabor" o "general" o null,
      "inicio": "HH:MM" en 24 horas,
      "fin": "HH:MM" en 24 horas,
      "pausa_minutos": 0
    }
  ],
  "minutos_totales": número o null
}

Reglas:
- Pregunta 7 = horario del envasado de VASITOS (no bandejas). Puede traer nombres: no los uses para nada.
- Pregunta 8 = cantidad de vasitos. Si hay desglose (Gourmet 434, Trisabor 415), SUMA tú el total en vasitos.
- "729", "742 vasitos", "Trisabor:415 Gourmet:434" → vasitos numérico.
- Horas: convierte a 24h. El envasado es de tarde: "3:20" → "15:20", "5:45pm" → "17:45", "6:15" → "18:15", "9:30" → "21:30".
- El tramo es la hora de inicio y la de fin de reloj. NO recortes el horario por pausas.
- Varios tramos (gourmet de X a Y, trisabor de A a B) van en "tramos" por separado, cada uno con su inicio y fin de reloj.
- pausa_minutos de cada tramo: siempre 0. Las pausas se preguntan aparte y no se restan acá.
- minutos_totales: suma de las duraciones de reloj, sin restar pausas, o null si no puedes.
- n_trabajadores: null. trabajadores: []. No cuentes personas a partir de los nombres.
- hubo_envasado false solo si dijeron que no se envasaron vasitos / cero vasitos.
- Typos y español de planta OK. No inventes horas que no estén.
"""


def _parse_json_objeto(raw: str) -> dict:
    s = (raw or "").strip()
    if not s:
        raise ValueError("respuesta vacía de Grok")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()
    data = json.loads(s)
    if not isinstance(data, dict):
        raise ValueError("JSON de envasado no es un objeto")
    return data


def _solicitar_extraccion_envasado(user_content: str) -> str:
    response = client.chat.completions.create(
        model="grok-4-1-fast",
        messages=[
            {"role": "system", "content": _PROMPT_EXTRACCION_ENVASADO},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        timeout=ENVASADO_EXTRACCION_TIMEOUT,
    )
    contenido = response.choices[0].message.content
    if not contenido or not str(contenido).strip():
        raise ValueError("respuesta vacía de Grok")
    return contenido


async def extraer_datos_envasado_vasitos(answers: dict) -> dict:
    """Grok solo extrae horas y vasitos. Reintenta una vez si falla o viene incompleto.

    Antes, cualquier error devolvía {} y el canal no se enteraba.
    """
    q7 = answers.get(str(IDX_VASITOS_HORAS), "")
    q8 = answers.get(str(IDX_VASITOS_CANTIDAD), "")
    user_content = f"""
Pregunta 7 (horario del envasado de vasitos):
\"\"\"{q7 or "(vacío)"}\"\"\"

Pregunta 8 (cantidad de vasitos):
\"\"\"{q8 or "(vacío)"}\"\"\"
"""
    ultimo: dict = {}
    for intento in (1, 2):
        try:
            raw = _solicitar_extraccion_envasado(user_content)
            data = _parse_json_objeto(raw)
            if _extraccion_envasado_completa(data, q8):
                return data
            print(f"Extracción de envasado incompleta (intento {intento}): {data!r}")
            ultimo = data
        except Exception as e:
            print(f"Error al extraer datos de envasado (intento {intento}): {e}")
            traceback.print_exc()
            ultimo = {}
        if intento == 1:
            await asyncio.sleep(ENVASADO_REINTENTO_ESPERA)
    return ultimo


def _guardar_calculo(state: dict, calc: dict, datos: dict) -> None:
    state["calculo_envasado"] = {k: v for k, v in calc.items() if k != "mensaje"}
    state["calculo_envasado_datos"] = datos


async def _armar_mensaje_calculo_envasado(state: dict) -> str:
    answers = state.get("answers") or {}
    q8 = answers.get(str(IDX_VASITOS_CANTIDAD), "")
    if _respuesta_sin_vasitos(q8):
        _guardar_calculo(
            state,
            {"ok": False, "motivo": "no_vasitos", "vasitos": 0},
            {},
        )
        return MENSAJE_SIN_VASITOS

    datos = await extraer_datos_envasado_vasitos(answers)
    if not isinstance(datos, dict):
        datos = {}
    q8_simple = _vasitos_entero_simple(q8)
    if q8_simple is not None:
        datos = dict(datos)
        datos["vasitos"] = q8_simple
        if q8_simple > 0:
            datos["hubo_envasado"] = True

    n = state.get("vasitos_n_personas")
    if n is None:
        n = _numero_personas(answers.get(str(IDX_VASITOS_PERSONAS), ""))
    pausa = state.get("vasitos_pausa_minutos")
    if pausa is None:
        pausa = _minutos_pausa(answers.get(str(IDX_VASITOS_PAUSAS), ""))
    detalle = state.get("vasitos_personas_detalle") or ""
    if not detalle:
        detalle = _detalle_personas(answers.get(str(IDX_VASITOS_PERSONAS), ""))

    if n is None or pausa is None or not _extraccion_envasado_completa(datos, q8):
        _guardar_calculo(
            state,
            {
                "ok": False,
                "motivo": "extraccion_incompleta",
                "mensaje": (
                    "No se pudieron interpretar las horas, la cantidad de vasitos "
                    "o los datos de la meta."
                ),
                "n_trabajadores": n,
                "pausa_minutos": pausa,
            },
            datos,
        )
        return _mensaje_fallo_meta_envasado(
            "No se pudieron interpretar las horas, la cantidad de vasitos o los datos de la meta."
        )

    calc = calcular_envasado_vasitos(
        datos,
        n_personas=n,
        pausa_minutos=pausa,
        personas_detalle=detalle,
    )
    if not calc:
        _guardar_calculo(
            state,
            {
                "ok": False,
                "motivo": "extraccion_vacia",
                "mensaje": "No se pudieron interpretar las horas ni la cantidad de vasitos.",
            },
            datos,
        )
        return MENSAJE_FALLO_META_ENVASADO
    _guardar_calculo(state, calc, datos)
    return formatear_calculo_envasado(calc)


async def mensaje_calculo_envasado(state: dict) -> str:
    """Texto para el canal. Si el cálculo no se arma, avisa a Tania en vez de callar."""
    try:
        return await _armar_mensaje_calculo_envasado(state)
    except Exception as e:
        print(f"Error en cálculo de envasado: {e}")
        traceback.print_exc()
        state["calculo_envasado"] = {"ok": False, "motivo": "excepcion"}
        return MENSAJE_FALLO_META_ENVASADO


async def iniciar_reporte(channel: discord.abc.Messageable, user: discord.abc.User):
    channel_id = str(channel.id)
    conversation_state[channel_id] = {
        "paso": 0,
        "user_id": user.id,
        "answers": {},
        "buffer": "",
        "sin_pedidos": False,
    }
    await channel.send(
        f"**Reporte de Cierre iniciado** por {user.mention}\n\n"
        f"{PREGUNTAS[0]['texto']}\n\n"
        f"_Solo {user.display_name} puede responder este reporte._\n"
        f"_Escribe **cancelar** o **cancelar reporte** para detenerlo._\n"
        f"_Si hoy **no se alistaron pedidos**, dilo en la pregunta 1 y se saltan las 2–5._"
    )


# ================== COMANDO SLASH ==================
@bot.tree.command(name="reporte-cierre", description="Inicia el Reporte de Cierre")
async def reporte_cierre_slash(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id in conversation_state:
        dueño = conversation_state[channel_id]["user_id"]
        await interaction.response.send_message(
            f"Ya hay un reporte de cierre en curso (iniciado por <@{dueño}>). "
            f"Termínenlo o escriban **cancelar reporte**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    await iniciar_reporte(interaction.channel, interaction.user)
    try:
        await interaction.followup.send("Reporte iniciado en el canal.", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} slash commands sincronizados")
    except Exception as e:
        print(f"❌ Error al sincronizar comandos: {e}")
        traceback.print_exc()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content_lower = (message.content or "").strip().lower()
    channel_id = str(message.channel.id)
    state = conversation_state.get(channel_id)

    # --- Cancelar (solo si hay reporte y es el dueño o un admin puede forzar) ---
    if state and _parece_cancelar(content_lower):
        # Dueño siempre; otros solo si es "cancelar reporte" explícito de supervisión
        es_dueño = message.author.id == state["user_id"]
        if es_dueño or content_lower in {"cancelar reporte", "reporte cancelado", "canelar reporte"}:
            del conversation_state[channel_id]
            await message.channel.send("✅ Reporte de Cierre cancelado.")
        return

    # --- Iniciar por texto ---
    if _parece_inicio(content_lower):
        if channel_id in conversation_state:
            dueño = conversation_state[channel_id]["user_id"]
            await message.channel.send(
                f"Ya hay un reporte en curso (iniciado por <@{dueño}>). "
                f"Escriban **cancelar reporte** para reiniciar."
            )
            return
        await iniciar_reporte(message.channel, message.author)
        return

    # --- Solo procesar si hay reporte activo ---
    if not state:
        return

    # --- Aislamiento: solo el dueño responde ---
    if message.author.id != state["user_id"]:
        # No reaccionar ni regañar: ignora mensajes de terceros (Tania, Martha, etc.)
        return

    await manejar_respuesta(message)


# ================== CONSULTA A GROK ==================
async def consultar_grok(
    pregunta_item: dict,
    respuesta_nueva: str,
    buffer: str,
    respuestas_previas: dict,
    sin_pedidos: bool,
    tiene_adjuntos: bool,
) -> dict:
    system_prompt = """
Eres el validador de un reporte de cierre de una fábrica de helados (Helados R's).
Tu trabajo es decidir si la respuesta del trabajador YA aporta lo esencial de la pregunta.

Responde ÚNICAMENTE con JSON válido:
{
  "respuesta_valida": true o false,
  "mensaje": null o "texto corto pidiendo SOLO lo que falta",
  "skip_bloque_pedidos": true o false
}

# Principios (OBLIGATORIOS)
1. Sé RAZONABLE, no legalista. Evalúa si la INFORMACIÓN está, no si la frase es perfecta.
2. Español informal de planta: "si", "iso", "se termino", "pinpinas", typos → acéptalos.
3. Si el BUFFER (respuestas previas a la MISMA pregunta) + la respuesta nueva ya completan lo pedido → válido.
   NO pidas de nuevo datos que ya están en el buffer.
4. "Sí" / "Si" es válido en preguntas de sí/no.
5. Medidas aproximadas son válidas: "medio tanque", "a la mitad", "5 rayas", "bien", "óptimo".
6. NUNCA digas genérico "Responde completo lo que se te preguntó" si puedes decir exactamente qué falta.
7. NUNCA rechaces una respuesta solo por no copiar la pregunta al pie de la letra.
8. Si la respuesta es un punto, vacía o basura → inválida. Mensaje: "Escribe la respuesta, no un punto."
9. Si hay adjunto (foto/video) y texto que describe niveles o confirma algo → suele ser válido.
10. Si hoy NO hubo / NO se alistaron / NO se hicieron pedidos:
    - respuesta_valida = true
    - skip_bloque_pedidos = true
    (salvo que la pregunta actual YA no sea del bloque de pedidos)

# Errores que DEBES evitar (casos reales)
- Rechazar "3.217 pote" por "no dijiste la cantidad" cuando el número está.
- En el aceite de la planta azul no rechaces "aceite bien", "óptimo" o "bajo" pidiendo gasoil o "medio tanque": el gasoil de la azul se contesta aparte, con un número de líneas.
- En la planta roja no rechaces "medio tanque y aceite bien" pidiendo el gasoil otra vez.
- Rechazar "10 de 25L y 4 de 30L" pidiendo "cantidad total".
- Rechazar "Yuselis empezó 5:45 hasta 6:45" pidiendo horas o nombres otra vez.
- Exigir nombres de quien alistó cuando dijeron "hoy no se hicieron pedidos".
- Exigir horas de alistamiento cuando no hubo pedidos.
- Forzar copiar "encendidos, bien cerrados y calientes" cuando dijeron "sí".
- Entrar en bucle: pedir nombres, luego horas, luego nombres otra vez.

# skip_bloque_pedidos
Pon true SOLO si la respuesta indica claramente que no hubo/no se alistaron pedidos hoy
y aún estamos en preguntas de pedidos (1-5). Si no aplica, false.
"""

    prev_txt = ""
    if respuestas_previas:
        lines = []
        for k in sorted(respuestas_previas.keys(), key=lambda x: int(x)):
            idx = int(k)
            if 0 <= idx < len(PREGUNTAS):
                lines.append(f"Q{idx + 1}: {respuestas_previas[k]}")
        prev_txt = "\n".join(lines)

    user_content = f"""
Pregunta actual:
{pregunta_item["texto"]}

Reglas específicas de ESTA pregunta:
{pregunta_item["reglas"]}

Estado: sin_pedidos_ya_detectado = {sin_pedidos}
Tiene_adjuntos_en_este_mensaje = {tiene_adjuntos}

Respuestas ya aceptadas de preguntas anteriores:
{prev_txt or "(ninguna)"}

BUFFER de la misma pregunta (intentos previos aún no aceptados del todo):
\"\"\"{buffer or "(vacío)"}\"\"\"

Respuesta NUEVA del trabajador:
\"\"\"{respuesta_nueva}\"\"\"

Evalúa BUFFER + respuesta nueva juntos. Si juntos bastan → respuesta_valida true.
"""

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        # Normalizar
        return {
            "respuesta_valida": bool(data.get("respuesta_valida", False)),
            "mensaje": data.get("mensaje"),
            "skip_bloque_pedidos": bool(data.get("skip_bloque_pedidos", False)),
        }
    except Exception as e:
        print(f"Error al consultar Grok: {e}")
        traceback.print_exc()
        # Fail-open solo si hay texto real; fail-closed si basura
        if _vacio_o_basura(respuesta_nueva, tiene_adjuntos):
            return {
                "respuesta_valida": False,
                "mensaje": "No pude validar. Escribe de nuevo la respuesta con más detalle.",
                "skip_bloque_pedidos": False,
            }
        return {
            "respuesta_valida": True,
            "mensaje": None,
            "skip_bloque_pedidos": False,
        }


# ================== MANEJO DE RESPUESTAS ==================
async def manejar_respuesta(message: discord.Message):
    channel_id = str(message.channel.id)
    state = conversation_state.get(channel_id)
    if not state:
        return

    paso = state["paso"]
    if paso < 0 or paso >= len(PREGUNTAS):
        del conversation_state[channel_id]
        return

    pregunta_item = PREGUNTAS[paso]
    texto = (message.content or "").strip()
    tiene_adjuntos = bool(message.attachments)

    if paso == IDX_BATIDORAS_FOTO:
        await _manejar_foto_batidoras(message, state, texto, tiene_adjuntos)
        return

    if paso in (
        IDX_PLANTA_AZUL_PRENDIDA,
        IDX_GASOIL_AZUL_LINEAS,
        IDX_GASOIL_AZUL_FOTO_NIVEL,
        IDX_GASOIL_AZUL_FOTO_RECARGA,
        IDX_ACEITE_AZUL_FOTO,
        IDX_PLANTA_AZUL_SWITCH,
    ):
        await _manejar_gasoil_planta_azul(message, state, texto, tiene_adjuntos)
        return

    if paso in (IDX_VASITOS_PERSONAS, IDX_VASITOS_PAUSAS):
        await _manejar_personas_y_pausas_vasitos(message, state, texto)
        return

    # Pre-filtro sin gastar tokens
    if _vacio_o_basura(texto, tiene_adjuntos):
        await message.channel.send(
            "Escribe la respuesta con datos (no uses solo un punto). "
            "Si no aplica, dilo con palabras: por ejemplo *hoy no se alistaron pedidos*."
        )
        return

    # Acumular buffer de la pregunta actual
    if state["buffer"]:
        buffer_completo = (state["buffer"] + "\n" + texto).strip()
    else:
        buffer_completo = texto

    # Nota de adjuntos en el texto enviado a Grok
    respuesta_para_ia = texto
    if tiene_adjuntos:
        nombres = ", ".join(a.filename for a in message.attachments)
        respuesta_para_ia = f"{texto}\n[Adjuntos: {nombres}]".strip()

    decision = await consultar_grok(
        pregunta_item=pregunta_item,
        respuesta_nueva=respuesta_para_ia,
        buffer=state["buffer"],
        respuestas_previas=state["answers"],
        sin_pedidos=state.get("sin_pedidos", False),
        tiene_adjuntos=tiene_adjuntos,
    )

    if not decision.get("respuesta_valida", False):
        # Guardar intento parcial para no perder info en el siguiente mensaje
        state["buffer"] = buffer_completo
        mensaje = decision.get("mensaje") or "Falta un dato. Completa solo lo que falta."
        # Evitar el genérico tóxico si viene vacío de sentido
        if mensaje.strip().lower() in {
            "responde completo lo que se te preguntó.",
            "responde completo lo que se te preguntó",
            "tu mensaje está incompleto.",
            "tu mensaje está incompleto",
        }:
            mensaje = "Me falta un dato concreto de la pregunta. Complétalo en un mensaje más."
        await message.channel.send(mensaje)
        return

    # ===== Válida =====
    try:
        await message.add_reaction("✅")
    except Exception:
        pass

    # Guardar respuesta (buffer + nueva)
    state["answers"][str(paso)] = buffer_completo
    state["buffer"] = ""

    # Saltar bloque pedidos si aplica
    if decision.get("skip_bloque_pedidos") and paso <= PEDIDOS_FIN:
        for i in range(paso, PEDIDOS_FIN + 1):
            if str(i) not in state["answers"]:
                state["answers"][str(i)] = (
                    buffer_completo if i == paso else "N/A — no se alistaron pedidos hoy"
                )
        _saltar_resto_pedidos(state)
        await message.channel.send(
            "Entendido: **hoy no se alistaron pedidos**. "
            "Salto las preguntas 2–5 del bloque de pedidos.\n\n"
            f"{PREGUNTAS[state['paso']]['texto']}"
        )
        return

    # Avanzar una pregunta
    paso_completado = paso
    state["paso"] += 1

    # Sin vasitos no hay meta: no se pregunta personas ni pausa.
    if paso_completado == IDX_VASITOS_CANTIDAD and _respuesta_sin_vasitos(
        state["answers"].get(str(IDX_VASITOS_CANTIDAD), "")
    ):
        state["paso"] = IDX_VASITOS_PAUSAS + 1
        state["calculo_envasado"] = {"ok": False, "motivo": "no_vasitos", "vasitos": 0}
        await message.channel.send(MENSAJE_SIN_VASITOS)

    if state["paso"] < len(PREGUNTAS):
        await message.channel.send(PREGUNTAS[state["paso"]]["texto"])
    else:
        await message.channel.send(
            "✅ **Reporte de Cierre completado.** ¡Gracias!\n"
            f"_Respondido por <@{state['user_id']}>._"
        )
        print(
            f"[CIERRE OK] channel={channel_id} "
            f"answers={json.dumps(state['answers'], ensure_ascii=False)} "
            f"calculo={json.dumps(state.get('calculo_envasado'), ensure_ascii=False)}"
        )
        del conversation_state[channel_id]


# ================== PERSONAS Y PAUSAS DEL ENVASADO ==================
_REPREGUNTA_PERSONAS = (
    "Para no contar mal los nombres, escribe el número de personas. "
    "Si quieres, agrega los nombres después "
    "(ejemplo: 2, José Luis y Ángel).\n\n"
)
_REPREGUNTA_PAUSAS = (
    "Escribe los minutos de pausa con un número. "
    "Si no pausaron, escribe 0.\n\n"
)


async def _manejar_personas_y_pausas_vasitos(
    message: discord.Message,
    state: dict,
    texto: str,
) -> None:
    paso = state["paso"]
    if paso == IDX_VASITOS_PERSONAS:
        decision = resolver_personas_envasado(state.get("buffer") or "", texto)
        aviso = _REPREGUNTA_PERSONAS
    else:
        decision = resolver_pausas_envasado(state.get("buffer") or "", texto)
        aviso = _REPREGUNTA_PAUSAS

    if not decision["ok"]:
        if not decision.get("vacio"):
            state["buffer"] = decision["buffer"]
        if decision.get("vacio"):
            aviso = "Escribe la respuesta con un número.\n\n"
        await message.channel.send(aviso + PREGUNTAS[paso]["texto"])
        return

    try:
        await message.add_reaction("✅")
    except Exception:
        pass

    state["answers"][str(paso)] = decision["respuesta"]
    state["buffer"] = ""

    if paso == IDX_VASITOS_PERSONAS:
        state["vasitos_n_personas"] = decision["n"]
        state["vasitos_personas_detalle"] = decision["detalle"]
        state["paso"] = IDX_VASITOS_PAUSAS
        await message.channel.send(PREGUNTAS[IDX_VASITOS_PAUSAS]["texto"])
        return

    state["vasitos_pausa_minutos"] = decision["pausa"]
    state["paso"] = paso + 1
    try:
        resumen = await mensaje_calculo_envasado(state)
    except Exception as e:
        print(f"Error en cálculo de envasado: {e}")
        traceback.print_exc()
        resumen = MENSAJE_FALLO_META_ENVASADO
    if not resumen:
        resumen = MENSAJE_FALLO_META_ENVASADO
    await message.channel.send(resumen)
    await _preguntar_o_cerrar(message, state)


# ================== INICIO ==================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
