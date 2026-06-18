import discord
from discord.ext import commands
import os
import traceback

print(">>> Iniciando bot de Reporte de Cierre...")

# ================== CONFIGURACIÓN ==================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise RuntimeError("❌ ERROR: Falta la variable de entorno DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

conversation_state = {}

# ================== COMANDO SLASH ==================
@bot.tree.command(name="reporte-cierre", description="Inicia el Reporte de Cierre")
async def reporte_cierre_slash(interaction: discord.Interaction):
    channel = interaction.channel
    conversation_state[str(channel.id)] = {"tipo": "cierre", "paso": 1}
    await interaction.response.send_message(
        "**Reporte de Cierre iniciado.**\n\n"
        "1. ¿Qué trabajadores alistaron los pedidos?\n\n"
        "_Escribe **cancelar** o **cancelar reporte** en cualquier momento para detenerlo._"
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

    # Detección mejorada de cancelar (funciona aunque escribas más texto)
    if "cancelar" in content:
        if channel_id in conversation_state:
            del conversation_state[channel_id]
            await message.channel.send("✅ Reporte cancelado.")
        return

    if content in ["reporte de cierre", "iniciar reporte de cierre"]:
        await iniciar_reporte_cierre(message.channel)

    elif channel_id in conversation_state:
        await manejar_respuesta(message)


# ================== INICIO DEL REPORTE ==================
async def iniciar_reporte_cierre(channel):
    conversation_state[str(channel.id)] = {"tipo": "cierre", "paso": 1}
    await channel.send(
        "**Reporte de Cierre iniciado.**\n\n"
        "1. ¿Qué trabajadores alistaron los pedidos?\n\n"
        "_Escribe **cancelar** o **cancelar reporte** en cualquier momento para detenerlo._"
    )


# ================== MANEJO DE RESPUESTAS ==================
async def manejar_respuesta(message):
    channel_id = str(message.channel.id)
    state = conversation_state[channel_id]
    tipo = state["tipo"]

    # Solo el emoji de check, nada más
    await message.channel.send("✅")

    if tipo == "cierre":
        paso = state["paso"]

        if paso == 1:
            state["paso"] = 2
            await message.channel.send("2. ¿A qué clientes pertenecen los pedidos que se alistaron?")
        elif paso == 2:
            state["paso"] = 3
            await message.channel.send("3. ¿Cómo quedaron los pedidos? (cuáles se completaron, cuáles quedaron pendientes, observaciones relevantes y cualquier ajuste para Producción mañana). En caso de que hayan pedidos incompletos, explica por qué quedaron incompletos.")
        elif paso == 3:
            state["paso"] = 4
            await message.channel.send("4. ¿En qué congeladores quedaron cuáles pedidos?")
        elif paso == 4:
            state["paso"] = 5
            await message.channel.send("5. ¿A qué hora se comenzaron a alistar los pedidos y a qué hora terminaron?")
        elif paso == 5:
            state["paso"] = 6
            await message.channel.send("6. ¿A qué hora empezaste y terminaste el envasado de bandejas? ¿Qué trabajadores se encargaron?")
        elif paso == 6:
            state["paso"] = 7
            await message.channel.send("7. ¿A qué hora empezaste y terminaste el envasado de vasitos? ¿Qué trabajadores se encargaron del envasado de vasitos?")
        elif paso == 7:
            state["paso"] = 8
            await message.channel.send("8. ¿Cuántos vasitos se envasaron en total hoy?")
        elif paso == 8:
            state["paso"] = 9
            await message.channel.send("9. ¿A qué hora empezaste y terminaste la limpieza? ¿Qué trabajadores se encargaron?")
        elif paso == 9:
            state["paso"] = 10
            await message.channel.send("10. ¿Limpiaste los tambores de las batidoras y retiraste la sal de los cabezotes?")
        elif paso == 10:
            state["paso"] = 11
            await message.channel.send("11. ¿Cuánto gasoil tiene ahora la planta azul y cuál es el nivel de aceite?")
        elif paso == 11:
            state["paso"] = 12
            await message.channel.send("12. ¿Cuánto gasoil tiene ahora la planta roja y cuál es el nivel de aceite?")
        elif paso == 12:
            state["paso"] = 13
            await message.channel.send("13. ¿Todos los motores de los congeladores están encendidos, bien cerrados y calientes?")
        elif paso == 13:
            state["paso"] = 14
            await message.channel.send("14. ¿Cerraste con candado las puertas de los heladeros, el estacionamiento, la planta y la salida (con doble candado)?")
        elif paso == 14:
            state["paso"] = 15
            await message.channel.send("15. ¿Quedó apagado el aire acondicionado de producción y el de ventas?")
        elif paso == 15:
            state["paso"] = 16
            await message.channel.send("16. ¿Quedó registrada la producción del día completa en la hoja de cálculo correspondiente? ¿Quién lo hizo?")
        elif paso == 16:
            state["paso"] = 17
            await message.channel.send("17. ¿Oprimiste el botón rojo del tablero de batidoras?")
        elif paso == 17:
            state["paso"] = 18
            await message.channel.send("18. ¿Dejaron todas las luces de la fábrica apagadas?")
        elif paso == 18:
            state["paso"] = 19
            await message.channel.send("19. ¿Qué cantidad de gasoil hay en el depósito?")
        else:
            await message.channel.send("✅ Reporte de Cierre completado. ¡Gracias!")
            del conversation_state[channel_id]


# ================== INICIO DEL BOT ==================
bot.run(DISCORD_TOKEN)
