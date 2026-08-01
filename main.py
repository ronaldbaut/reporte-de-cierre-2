import discord
from discord.ext import commands
import os
import traceback
from openai import OpenAI
import json

print(">>> Iniciando bot de Reporte de Cierre...")

# ================== CONFIGURACIÓN ==================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("❌ ERROR: Falta la variable de entorno DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Cliente de xAI (Grok)
client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

conversation_state = {}

# ================== LISTA DE PREGUNTAS ==================
PREGUNTAS = [
    "1. ¿Qué trabajadores alistaron los pedidos?",
    "2. ¿A qué clientes pertenecen los pedidos que se alistaron?",
    "3. ¿Cómo quedaron los pedidos? (cuáles se completaron, cuáles quedaron pendientes, observaciones relevantes y cualquier ajuste para Producción mañana). En caso de que hayan pedidos incompletos, explica por qué quedaron incompletos.",
    "4. ¿En qué congeladores quedaron cuáles pedidos?",
    "5. ¿A qué hora se comenzaron a alistar los pedidos y a qué hora terminaron?",
    "6. ¿A qué hora empezaste y terminaste el envasado de bandejas? ¿Qué trabajadores se encargaron?",
    "7. ¿A qué hora empezaste y terminaste el envasado de vasitos? ¿Qué trabajadores se encargaron del envasado de vasitos?",
    "8. ¿Cuántos vasitos se envasaron en total hoy?",
    "9. ¿A qué hora empezaste y terminaste la limpieza? ¿Qué trabajadores se encargaron?",
    "10. ¿Limpiaste los tambores de las batidoras y retiraste la sal de los cabezotes?",
    "11. ¿Cuánto gasoil tiene ahora la planta azul y cuál es el nivel de aceite?",
    "12. ¿Cuánto gasoil tiene ahora la planta roja y cuál es el nivel de aceite?",
    "13. ¿Todos los motores de los congeladores están encendidos, bien cerrados y calientes?",
    "14. ¿Cerraste con candado las puertas de los heladeros, el estacionamiento, la planta y la salida (con doble candado)?",
    "15. ¿Quedó apagado el aire acondicionado de producción y el de ventas?",
    "16. ¿Quedó registrada la producción del día completa en la hoja de cálculo correspondiente? ¿Quién lo hizo?",
    "17. ¿Oprimiste el botón rojo del tablero de batidoras?",
    "18. ¿Dejaron todas las luces de la fábrica apagadas?",
    "19. ¿Qué cantidad de gasoil hay en el depósito?"
]

# ================== COMANDO SLASH ==================
@bot.tree.command(name="reporte-cierre", description="Inicia el Reporte de Cierre")
async def reporte_cierre_slash(interaction: discord.Interaction):
    channel = interaction.channel
    conversation_state[str(channel.id)] = {"paso": 0}
    await interaction.response.send_message(
        f"**Reporte de Cierre iniciado.**\n\n"
        f"{PREGUNTAS[0]}\n\n"
        f"_Escribe **cancelar** o **cancelar reporte** en cualquier momento para detenerlo._"
    )

@bot.event
async def on_ready():
    print(f'✅ Bot conectado como {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} slash commands sincronizados correctamente")
    except Exception as e:
        print(f"❌ Error al sincronizar comandos: {e}")
        traceback.print_exc()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.strip().lower()
    channel_id = str(message.channel.id)

    # Cancelación flexible
    if any(palabra in content for palabra in ["cancelar", "canelar", "cancel", "cancela", "cancelar reporte", "canelar reporte"]):
        if channel_id in conversation_state:
            del conversation_state[channel_id]
            await message.channel.send("✅ Reporte cancelado.")
        return

    if content in ["reporte de cierre", "iniciar reporte de cierre"]:
        conversation_state[channel_id] = {"paso": 0}
        await message.channel.send(
            f"**Reporte de Cierre iniciado.**\n\n"
            f"{PREGUNTAS[0]}\n\n"
            f"_Escribe **cancelar** o **cancelar reporte** en cualquier momento para detenerlo._"
        )
        return

    if channel_id in conversation_state:
        await manejar_respuesta(message)

# ================== FUNCIÓN QUE CONSULTA A GROK ==================
async def consultar_grok(pregunta: str, respuesta_usuario: str) -> dict:
    system_prompt = """
Eres un supervisor estricto de reportes de cierre de una fábrica de helados.

Responde ÚNICAMENTE con un JSON válido:

{
  "respuesta_valida": true o false,
  "mensaje": null o "texto"
}

Reglas:
- Si la respuesta es muy corta, vacía, irrelevante o no responde claramente la pregunta → respuesta_valida = false
- Cuando sea inválida, el campo "mensaje" debe decir de forma clara, simple y contundente qué falta.
  Ejemplos:
  - "Te falta decirme los nombres de los trabajadores."
  - "No me dijiste a qué hora empezaron y terminaron."
  - "Responde completo lo que se te preguntó."
- Sé directo y corto.
- Si la respuesta es válida, pon "mensaje": null
"""

    user_content = f"""
Pregunta actual:
{pregunta}

Respuesta del trabajador:
"{respuesta_usuario}"
"""

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Error al consultar Grok: {e}")
        return {
            "respuesta_valida": True,
            "mensaje": None
        }

# ================== MANEJO DE RESPUESTAS ==================
async def manejar_respuesta(message):
    channel_id = str(message.channel.id)
    state = conversation_state[channel_id]
    
    pregunta_actual = PREGUNTAS[state["paso"]]
    
    # Reacción en vez de mensaje ✅
    try:
        await message.add_reaction("✅")
    except:
        pass

    # Consultamos a Grok
    decision = await consultar_grok(pregunta_actual, message.content)

    # Si la respuesta no es válida → regaño y no avanzamos
    if not decision.get("respuesta_valida", True):
        mensaje = decision.get("mensaje") or "Tu respuesta está incompleta. Responde correctamente lo que se te preguntó."
        await message.channel.send(mensaje)
        return

    # ===== Respuesta válida → avanzamos =====
    state["paso"] += 1
    
    if state["paso"] < len(PREGUNTAS):
        await message.channel.send(PREGUNTAS[state["paso"]])
    else:
        await message.channel.send("✅ **Reporte de Cierre completado.** ¡Gracias!")
        del conversation_state[channel_id]

# ================== INICIO DEL BOT ==================
bot.run(DISCORD_TOKEN)
