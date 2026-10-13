import discord
from discord.ext import commands
import os
import traceback
from openai import OpenAI
import json
import re
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
#     "gasoil_azul_lineas": int,  # líneas ya respondidas de la planta azul
#   }
# }
conversation_state = {}

# Índices de preguntas del bloque de pedidos (0-based)
PEDIDOS_INICIO = 0
PEDIDOS_FIN = 4  # preguntas 1..5 inclusive

# Preguntas de envasado de vasitos (0-based): 7 = horas+personas, 8 = cantidad
IDX_VASITOS_HORAS = 6
IDX_VASITOS_CANTIDAD = 7

# Gasoil planta azul (0-based): líneas, foto del nivel y, si hay menos de 4,
# foto después de recargar. No desplaza los índices de pedidos ni de vasitos.
IDX_GASOIL_AZUL_LINEAS = 10
IDX_GASOIL_AZUL_FOTO_NIVEL = 11
IDX_GASOIL_AZUL_FOTO_RECARGA = 12
GASOIL_AZUL_MIN_LINEAS = 4

# Aceite planta azul, justo después del gasoil (la foto de recarga se salta
# si hay 4 líneas o más, y esta pregunta queda como la siguiente).
IDX_ACEITE_AZUL = IDX_GASOIL_AZUL_FOTO_RECARGA + 1
IDX_ACEITE_AZUL_FOTO = IDX_ACEITE_AZUL + 1

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
        "texto": "¿Cuántas líneas de gasoil tiene la planta azul?",
        "reglas": """
- Válido SOLO si la respuesta es un número entero ≥ 0 (0, 1, 2, 3, 4, …).
- Inválido: texto, decimales, negativos, "medio tanque", "5 rayas", "cinco", "4 líneas".
- No pidas nivel de aceite: esta pregunta es solo el número de líneas.
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


def _entero_no_negativo(texto: str) -> Optional[int]:
    """Entero ≥ 0, o None si el mensaje no es solo ese número."""
    t = (texto or "").strip()
    if not re.fullmatch(r"\d+", t):
        return None
    return int(t)


def _respuesta_con_adjuntos(texto: str, nombres_adjuntos) -> str:
    """Mismo formato que ya se arma para la validación: texto + [Adjuntos: …]."""
    cuerpo = (texto or "").strip()
    if not nombres_adjuntos:
        return cuerpo
    nota = "[Adjuntos: " + ", ".join(nombres_adjuntos) + "]"
    if cuerpo:
        return f"{cuerpo}\n{nota}"
    return nota


def _siguiente_tras_foto_nivel_gasoil(lineas: int):
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


async def _manejar_gasoil_planta_azul(
    message: discord.Message,
    state: dict,
    texto: str,
    tiene_adjuntos: bool,
) -> None:
    paso = state["paso"]

    if paso == IDX_GASOIL_AZUL_LINEAS:
        n = _entero_no_negativo(texto)
        if n is None:
            if texto and not _vacio_o_basura(texto, False):
                if state["buffer"]:
                    state["buffer"] = (state["buffer"] + "\n" + texto).strip()
                else:
                    state["buffer"] = texto
            await message.channel.send(
                "Tiene que ser un número entero, 0 o más.\n\n"
                f"{PREGUNTAS[IDX_GASOIL_AZUL_LINEAS]['texto']}"
            )
            return
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        state["answers"][str(paso)] = str(n)
        state["gasoil_azul_lineas"] = n
        state["buffer"] = ""
        state["paso"] = IDX_GASOIL_AZUL_FOTO_NIVEL
        await message.channel.send(PREGUNTAS[IDX_GASOIL_AZUL_FOTO_NIVEL]["texto"])
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
            lineas = _entero_no_negativo(
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
- Rechazar "medio tanque y aceite bien" pidiendo gasoil otra vez.
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
        IDX_GASOIL_AZUL_LINEAS,
        IDX_GASOIL_AZUL_FOTO_NIVEL,
        IDX_GASOIL_AZUL_FOTO_RECARGA,
        IDX_ACEITE_AZUL_FOTO,
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
