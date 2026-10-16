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

# Preguntas de envasado de vasitos (0-based): 7 = horas+personas, 8 = cantidad
IDX_VASITOS_HORAS = 6
IDX_VASITOS_CANTIDAD = 7

# Planta azul (0-based). No desplaza los índices de pedidos ni de vasitos.
# Prendida → líneas de antes de prenderla, y se saltan fotos, aceite y switch.
# Apagada → líneas, foto del nivel, foto de recarga si < 4, aceite, foto de
# aceite y, al final, el switch de la llave. La planta roja va justo después.
IDX_PLANTA_AZUL_PRENDIDA = 10
IDX_GASOIL_AZUL_LINEAS = 11
IDX_GASOIL_AZUL_FOTO_NIVEL = 12
IDX_GASOIL_AZUL_FOTO_RECARGA = 13
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
                "Tiene que ser un número entero, 0 o más.\n\n"
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
        return

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


def calcular_envasado_vasitos(datos: dict) -> Optional[dict]:
    """
    datos extraídos por Grok:
      hubo_envasado, vasitos, n_trabajadores, trabajadores, tramos[{inicio,fin,pausa_minutos}]
    El promedio es tiempo de reloj / vasitos (no minutos-persona).
    """
    if not datos or not datos.get("hubo_envasado", True):
        return None

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

    n = datos.get("n_trabajadores")
    try:
        n = int(n) if n is not None else 0
    except (TypeError, ValueError):
        n = 0
    nombres = datos.get("trabajadores") or []
    if isinstance(nombres, str):
        nombres = [nombres]
    nombres = [str(x).strip() for x in nombres if str(x).strip()]
    if n <= 0:
        n = len(nombres)

    minutos = 0.0
    tramos_ok = []
    for tramo in datos.get("tramos") or []:
        if not isinstance(tramo, dict):
            continue
        dur = _duracion_minutos(tramo.get("inicio") or "", tramo.get("fin") or "")
        if dur is None:
            continue
        try:
            pausa = float(tramo.get("pausa_minutos") or 0)
        except (TypeError, ValueError):
            pausa = 0
        neto = max(0.0, dur - pausa)
        if neto <= 0:
            continue
        tramos_ok.append(neto)
        minutos += neto

    if minutos <= 0:
        # Fallback si Grok mandó minutos_totales ya restando pausas
        try:
            minutos = float(datos.get("minutos_totales") or 0)
        except (TypeError, ValueError):
            minutos = 0
    if minutos <= 0:
        return {
            "ok": False,
            "motivo": "sin_tiempo",
            "mensaje": (
                "Tengo el total de vasitos, pero no pude armar el tiempo de envasado "
                "con las horas. El promedio no se calculó."
            ),
        }

    segundos = minutos * 60.0
    seg_por_vasito = segundos / vasitos
    if not isfinite(seg_por_vasito) or seg_por_vasito <= 0:
        return {
            "ok": False,
            "motivo": "calculo",
            "mensaje": "No pude calcular el promedio de segundos por vasito.",
        }

    meta = _meta_para_personas(n) if n else None
    cumplio = None
    if meta is not None:
        cumplio = seg_por_vasito <= meta + 1e-9

    return {
        "ok": True,
        "vasitos": vasitos,
        "n_trabajadores": n,
        "trabajadores": nombres,
        "minutos": minutos,
        "segundos": segundos,
        "seg_por_vasito": seg_por_vasito,
        "meta": meta,
        "cumplio": cumplio,
    }


def formatear_calculo_envasado(calc: dict) -> str:
    if not calc:
        return ""
    if not calc.get("ok"):
        return f"📊 **Envasado de vasitos:** {calc.get('mensaje')}"

    nombres = calc.get("trabajadores") or []
    quienes = f" ({', '.join(nombres)})" if nombres else ""
    n = calc.get("n_trabajadores") or 0
    personas = f"{n} persona" + ("" if n == 1 else "s") + quienes

    lineas = [
        "📊 **Cálculo de envasado de vasitos**",
        f"• Tiempo: {_fmt_duracion(calc['minutos'])} ({int(round(calc['segundos']))} s)",
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
    return "\n".join(lineas)


async def extraer_datos_envasado_vasitos(answers: dict) -> dict:
    """Grok solo extrae datos; el promedio y la meta se calculan en Python."""
    q7 = answers.get(str(IDX_VASITOS_HORAS), "")
    q8 = answers.get(str(IDX_VASITOS_CANTIDAD), "")
    system_prompt = """
Eres extractor de datos de un reporte de cierre (fábrica de helados).
NO calcules promedios ni metas. Solo extrae lo que dijeron.

Responde ÚNICAMENTE JSON:
{
  "hubo_envasado": true o false,
  "vasitos": número entero (0 si no hubo),
  "n_trabajadores": número entero,
  "trabajadores": ["Nombre", ...],
  "tramos": [
    {
      "etiqueta": "gourmet" o "trisabor" o "general" o null,
      "inicio": "HH:MM" en 24 horas,
      "fin": "HH:MM" en 24 horas,
      "pausa_minutos": número (0 si no hay pausa)
    }
  ],
  "minutos_totales": número o null
}

Reglas:
- Pregunta 7 = horas y quiénes envasaron VASITOS (no bandejas).
- Pregunta 8 = cantidad de vasitos. Si hay desglose (Gourmet 434, Trisabor 415), SUMA tú el total en vasitos.
- "729", "742 vasitos", "Trisabor:415 Gourmet:434" → vasitos numérico.
- Horas: convierte a 24h. El envasado es de tarde: "3:20" → "15:20", "5:45pm" → "17:45".
- Varios tramos (gourmet de X a Y, trisabor de A a B) van en "tramos" por separado.
- Si hay pausa ("paramos 15 min", "de 4 a 4:20 no se envasó"), ponla en pausa_minutos de ese tramo o reparte.
- minutos_totales: suma de tramos menos pausas, o null si no puedes.
- n_trabajadores: cuántas personas distintas envasaron vasitos. "Angel, Luis y Yuselis" = 3.
- hubo_envasado false si dijeron que no se envasaron vasitos / cero vasitos.
- Typos y español de planta OK. No inventes horas ni nombres que no estén.
"""
    user_content = f"""
Pregunta 7 (horas y trabajadores del envasado de vasitos):
\"\"\"{q7 or "(vacío)"}\"\"\"

Pregunta 8 (cantidad de vasitos):
\"\"\"{q8 or "(vacío)"}\"\"\"
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
        data = json.loads(response.choices[0].message.content)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        print(f"Error al extraer datos de envasado: {e}")
        traceback.print_exc()
        return {}


async def mensaje_calculo_envasado(state: dict) -> Optional[str]:
    datos = await extraer_datos_envasado_vasitos(state.get("answers") or {})
    calc = calcular_envasado_vasitos(datos)
    if not calc:
        return None
    state["calculo_envasado"] = {
        k: v for k, v in calc.items() if k != "mensaje"
    }
    state["calculo_envasado_datos"] = datos
    return formatear_calculo_envasado(calc)


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

    # Con horas (Q7) + total de vasitos (Q8) ya se puede calcular la meta
    if paso_completado == IDX_VASITOS_CANTIDAD:
        try:
            resumen = await mensaje_calculo_envasado(state)
        except Exception as e:
            print(f"Error en cálculo de envasado: {e}")
            traceback.print_exc()
            resumen = None
        if resumen:
            await message.channel.send(resumen)

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


# ================== INICIO ==================
bot.run(DISCORD_TOKEN)
