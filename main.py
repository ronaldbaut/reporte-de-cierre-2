import discord
from discord.ext import commands
import os
import traceback
from openai import OpenAI
import json
import re
 
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
#   }
# }
conversation_state = {}
 
# Índices de preguntas del bloque de pedidos (0-based)
PEDIDOS_INICIO = 0
PEDIDOS_FIN = 4  # preguntas 1..5 inclusive
 
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
""",
    },
    {
        "texto": "8. ¿Cuántos vasitos se envasaron en total hoy?",
        "reglas": """
- Válido: un total numérico (729, 742 vasitos) O desglose que sume (Trisabor:415 Gourmet:434).
- NO exijas "total" si ya dio números claros de vasitos.
- Inválido: solo horas de envasado sin cantidad.
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
        "texto": "11. ¿Cuánto gasoil tiene ahora la planta azul y cuál es el nivel de aceite?",
        "reglas": """
- Válido: cantidad o nivel de gasoil (rayas, líneas, mitad, pimpina, número) Y algo del aceite (bien / óptimo / bajo / foto).
- "5 rayas de gasoil y aceite bien" ES válido.
- "Medio tanque y aceite bien" ES válido (no exijas número exacto de litros).
- Si la planta está en uso y no puede medir, acepta esa explicación + lo que sí sepa.
- Foto + texto corto sobre niveles ES válido.
""",
    },
    {
        "texto": "12. ¿Cuánto gasoil tiene ahora la planta roja y cuál es el nivel de aceite?",
        "reglas": """
- Igual que planta azul.
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
    state["paso"] += 1
 
    if state["paso"] < len(PREGUNTAS):
        await message.channel.send(PREGUNTAS[state["paso"]]["texto"])
    else:
        await message.channel.send(
            "✅ **Reporte de Cierre completado.** ¡Gracias!\n"
            f"_Respondido por <@{state['user_id']}>._"
        )
        # Aquí podrías persistir state["answers"] a Notion/Sheets/archivo
        print(f"[CIERRE OK] channel={channel_id} answers={json.dumps(state['answers'], ensure_ascii=False)}")
        del conversation_state[channel_id]
 
 
# ================== INICIO ==================
bot.run(DISCORD_TOKEN)
