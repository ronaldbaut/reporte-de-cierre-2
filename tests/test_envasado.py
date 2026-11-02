"""Cálculo de envasado de vasitos, sin red y sin levantar el bot.

Corre desde la raíz del repo:

    python -m unittest tests.test_envasado
"""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")

import main


def _datos_horario(inicio="6:15", fin="9:30", vasitos=673, n_grok=3, pausa_tramo=40):
    """Lo que Grok devolvía mal el 30/10: contaba personas y restaba pausa del tramo."""
    return {
        "hubo_envasado": True,
        "vasitos": vasitos,
        "n_trabajadores": n_grok,
        "trabajadores": ["José", "Luis", "Ángel"],
        "tramos": [
            {
                "inicio": inicio,
                "fin": fin,
                "pausa_minutos": pausa_tramo,
            }
        ],
        "minutos_totales": 1,
    }


class _Canal:
    def __init__(self):
        self.id = 555
        self.mensajes = []

    async def send(self, content):
        self.mensajes.append(content)


class _Autor:
    def __init__(self, user_id=7):
        self.id = user_id
        self.bot = False


class _Adjunto:
    def __init__(self, filename):
        self.filename = filename


class _Msg:
    def __init__(self, content, canal, user_id=7, attachments=None):
        self.content = content
        self.channel = canal
        self.author = _Autor(user_id)
        self.attachments = list(attachments or [])
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def _estado(paso, **extra):
    state = {
        "paso": paso,
        "user_id": 7,
        "answers": {},
        "buffer": "",
        "sin_pedidos": False,
    }
    state.update(extra)
    return state


async def _grok_valido(*_args, **_kwargs):
    return {"respuesta_valida": True, "mensaje": None, "skip_bloque_pedidos": False}


class PreguntasYMetas(unittest.TestCase):
    def test_el_resto_del_reporte_sigue_en_su_lugar(self):
        textos = [p["texto"] for p in main.PREGUNTAS]
        self.assertEqual(len(textos), 31)
        prefijos = [
            "1. ¿Qué trabajadores alistaron los pedidos?",
            "2. ¿A qué clientes pertenecen los pedidos",
            "3. ¿Cómo quedaron los pedidos?",
            "4. ¿En qué congeladores quedaron cuáles pedidos?",
            "5. ¿A qué hora se comenzaron a alistar los pedidos",
            "6. ¿A qué hora empezaste y terminaste el envasado de bandejas?",
            "7. ¿A qué hora empezaste y terminaste el envasado de vasitos?",
            "8. ¿Cuántos vasitos se envasaron en total hoy?",
            "9. ¿A qué hora empezaste y terminaste la limpieza?",
            "10. ¿Limpiaste los tambores de las batidoras",
            "11. ¿Cuál es el nivel de aceite de la planta azul?",
            "12. ¿Cuánto gasoil tiene ahora la planta roja",
            "13. ¿Todos los motores de los congeladores",
            "14. ¿Cerraste con candado",
            "15. ¿Quedó apagado el aire acondicionado",
            "16. ¿Quedó registrada la producción del día",
            "17. ¿Oprimiste el botón rojo",
            "18. ¿Dejaron todas las luces",
            "19. ¿Qué cantidad de gasoil hay en el depósito?",
            "20. ¿Recibiste todos los implementos de ventas",
            "21. ¿Qué faltó o sobró de:",
            "22. ¿Enviaste el mensaje de lo que llegó?",
        ]
        for prefijo in prefijos:
            self.assertEqual(sum(1 for t in textos if t.startswith(prefijo)), 1, prefijo)

        self.assertTrue(textos[main.IDX_VASITOS_HORAS].startswith("7. "))
        self.assertTrue(textos[main.IDX_VASITOS_CANTIDAD].startswith("8. "))
        self.assertIn("Cuántas personas envasaron", textos[main.IDX_VASITOS_PERSONAS])
        self.assertIn("minutos pausaron", textos[main.IDX_VASITOS_PAUSAS])
        self.assertTrue(
            textos[main.IDX_VASITOS_PAUSAS + 1].startswith(
                "9. ¿A qué hora empezaste y terminaste la limpieza?"
            )
        )
        self.assertTrue(
            textos[main.IDX_BATIDORAS_FOTO - 1].startswith(
                "10. ¿Limpiaste los tambores de las batidoras"
            )
        )
        self.assertEqual(textos[main.IDX_BATIDORAS_FOTO], main.TEXTO_BATIDORAS_FOTO)
        self.assertEqual(textos[main.IDX_BATIDORAS_FOTO + 1], main.TEXTO_PLANTA_AZUL_PRENDIDA)
        self.assertEqual(
            textos[main.IDX_PLANTA_AZUL_PRENDIDA], main.TEXTO_PLANTA_AZUL_PRENDIDA
        )
        self.assertEqual(textos[main.IDX_GASOIL_AZUL_LINEAS], main.TEXTO_GASOIL_AZUL_LINEAS)
        self.assertEqual(
            textos[main.IDX_GASOIL_AZUL_FOTO_NIVEL],
            "Envía una foto del nivel de gasoil de la planta azul.",
        )
        self.assertEqual(
            textos[main.IDX_ACEITE_AZUL],
            "11. ¿Cuál es el nivel de aceite de la planta azul?",
        )
        self.assertEqual(textos[main.IDX_PLANTA_AZUL_SWITCH], main.TEXTO_PLANTA_AZUL_SWITCH)
        self.assertTrue(textos[main.IDX_PLANTA_AZUL_SWITCH + 1].startswith("12. ¿Cuánto gasoil"))

    def test_horario_del_30_de_octubre(self):
        # 6:15 a 9:30 en el turno de la tarde son 3 h 15 min.
        self.assertEqual(main._duracion_minutos("6:15", "9:30"), 195)
        self.assertEqual(main._duracion_minutos("18:15", "21:30"), 195)

    def test_nombres_sin_numero_no_cuentan_personas(self):
        self.assertIsNone(main._numero_personas("José Luis y ángel"))
        self.assertIsNone(main._numero_personas("José Luis y Ángel"))
        self.assertIsNone(main._numero_personas("José, Luis y Ángel"))
        self.assertEqual(main._numero_personas("2, José Luis y Ángel"), 2)
        self.assertEqual(main._numero_personas("3, José, Luis y Ángel"), 3)
        self.assertEqual(main._numero_personas("2"), 2)
        self.assertIsNone(main._numero_personas("2 o 3"))
        self.assertIsNone(main._numero_personas("0"))
        self.assertIn("José Luis", main._detalle_personas("2, José Luis y Ángel"))
        self.assertNotIn("2", main._detalle_personas("2, José Luis y Ángel"))

    def test_pausa_en_minutos(self):
        self.assertEqual(main._minutos_pausa("0"), 0)
        self.assertEqual(main._minutos_pausa("no"), 0)
        self.assertEqual(main._minutos_pausa("sin pausa"), 0)
        self.assertEqual(main._minutos_pausa("15"), 15)
        self.assertEqual(main._minutos_pausa("15 minutos"), 15)
        self.assertIsNone(main._minutos_pausa("1 hora"))
        self.assertIsNone(main._minutos_pausa("15 y 20"))
        self.assertIsNone(main._minutos_pausa("673"))

    def test_sin_vasitos(self):
        self.assertTrue(main._respuesta_sin_vasitos("0"))
        self.assertTrue(main._respuesta_sin_vasitos("no se envasaron vasitos"))
        self.assertTrue(main._respuesta_sin_vasitos("ninguno"))
        self.assertFalse(main._respuesta_sin_vasitos("673"))
        self.assertFalse(main._respuesta_sin_vasitos("Trisabor:415 Gourmet:258"))
        self.assertEqual(main._vasitos_entero_simple("673"), 673)
        self.assertEqual(main._vasitos_entero_simple("742 vasitos"), 742)
        self.assertIsNone(main._vasitos_entero_simple("Trisabor:415 Gourmet:258"))

    def test_buffer_de_personas_no_se_traba(self):
        primero = main.resolver_personas_envasado("", "José Luis y ángel")
        self.assertFalse(primero["ok"])
        self.assertEqual(primero["buffer"], "José Luis y ángel")

        seguido = main.resolver_personas_envasado(primero["buffer"], "2")
        self.assertTrue(seguido["ok"])
        self.assertEqual(seguido["n"], 2)
        self.assertIn("ángel", seguido["detalle"])

        # Si el acumulado queda ambiguo, vale el último mensaje claro.
        ambiguo = main.resolver_personas_envasado("2 o 3", "2, José Luis y Ángel")
        self.assertTrue(ambiguo["ok"])
        self.assertEqual(ambiguo["n"], 2)

    def test_meta_usa_el_numero_explicito_no_el_de_grok(self):
        datos = _datos_horario()
        con_dos = main.calcular_envasado_vasitos(
            datos, n_personas=2, pausa_minutos=0, personas_detalle="José Luis y Ángel"
        )
        con_tres = main.calcular_envasado_vasitos(
            datos, n_personas=3, pausa_minutos=0, personas_detalle="José, Luis y Ángel"
        )
        self.assertTrue(con_dos["ok"])
        self.assertEqual(con_dos["n_trabajadores"], 2)
        self.assertEqual(con_dos["meta"], 26)
        self.assertEqual(con_tres["meta"], 18)
        self.assertEqual(con_dos["vasitos"], 673)
        self.assertAlmostEqual(con_dos["minutos_brutos"], 195)
        # La pausa que Grok metió en el tramo (40) no se descuenta si dijeron 0.
        self.assertEqual(con_dos["pausa_minutos"], 0)
        self.assertAlmostEqual(con_dos["minutos"], 195)
        self.assertAlmostEqual(con_dos["seg_por_vasito"], 11700 / 673, places=4)
        self.assertTrue(con_dos["cumplio"])
        self.assertTrue(con_tres["cumplio"])

        texto = main.formatear_calculo_envasado(con_dos)
        self.assertIn("Tiempo bruto: 3 h 15 min (11700 s)", texto)
        self.assertIn("Pausa descontada: 0 min", texto)
        self.assertIn("Tiempo neto: 3 h 15 min (11700 s)", texto)
        self.assertIn("673", texto)
        self.assertIn("José Luis y Ángel", texto)
        self.assertIn("17,4 s/vasito", texto)
        self.assertIn("Meta (2 personas): **26 s/vasito**", texto)
        self.assertIn("Cumplieron la meta", texto)
        self.assertNotIn("Tania", texto)

    def test_la_pausa_baja_el_tiempo_y_queda_visible_para_tania(self):
        datos = _datos_horario()
        sin_pausa = main.calcular_envasado_vasitos(datos, n_personas=4, pausa_minutos=0)
        con_pausa = main.calcular_envasado_vasitos(datos, n_personas=4, pausa_minutos=45)
        self.assertFalse(sin_pausa["cumplio"])
        self.assertEqual(sin_pausa["meta"], 14)
        self.assertTrue(con_pausa["cumplio"])
        self.assertAlmostEqual(con_pausa["minutos_brutos"], 195)
        self.assertEqual(con_pausa["pausa_minutos"], 45)
        self.assertAlmostEqual(con_pausa["minutos"], 150)
        self.assertAlmostEqual(con_pausa["seg_por_vasito"], 9000 / 673, places=4)

        texto = main.formatear_calculo_envasado(con_pausa)
        self.assertIn("Tiempo bruto: 3 h 15 min", texto)
        self.assertIn("Pausa descontada: 45 min", texto)
        self.assertIn("Tiempo neto: 2 h 30 min", texto)
        self.assertIn("Meta (4 o más personas): **14 s/vasito**", texto)
        self.assertIn("Tania", texto)
        self.assertIn("aprobación", texto)
        self.assertIn("tiempo bruto", texto)

        cinco = main.calcular_envasado_vasitos(datos, n_personas=5, pausa_minutos=0)
        self.assertEqual(cinco["meta"], 14)

    def test_pausa_que_cubre_el_turno_avisa_a_tania_con_los_minutos(self):
        datos = _datos_horario()
        calc = main.calcular_envasado_vasitos(datos, n_personas=2, pausa_minutos=200)
        self.assertFalse(calc["ok"])
        texto = main.formatear_calculo_envasado(calc)
        self.assertIn("Tania", texto)
        self.assertIn("3 h 15 min", texto)
        self.assertIn("3 h 20 min", texto)
        self.assertNotIn("s/vasito", texto)

    def test_extraccion_vacia_ya_no_es_silencio(self):
        # Antes calcular({}) devolvía None y el canal no publicaba nada.
        calc = main.calcular_envasado_vasitos({})
        self.assertIsNotNone(calc)
        self.assertFalse(calc["ok"])
        texto = main.formatear_calculo_envasado(calc)
        self.assertIn("No pude calcular la meta de envasado", texto)
        self.assertIn("Tania", texto)

        sin = main.formatear_calculo_envasado(
            {
                "ok": False,
                "motivo": "no_vasitos",
                "mensaje": "No se envasaron vasitos hoy: no hay promedio que calcular.",
            }
        )
        self.assertNotIn("Tania", sin)
        self.assertIn("No se envasaron vasitos hoy", sin)

    def test_una_persona_no_inventa_meta(self):
        calc = main.calcular_envasado_vasitos(
            _datos_horario(), n_personas=1, pausa_minutos=0
        )
        self.assertTrue(calc["ok"])
        self.assertIsNone(calc["meta"])
        texto = main.formatear_calculo_envasado(calc)
        self.assertIn("no define", texto)
        self.assertNotIn("Tania", texto)

    def test_json_con_cerco(self):
        data = main._parse_json_objeto('```json\n{"vasitos": 673}\n```')
        self.assertEqual(data["vasitos"], 673)
        with self.assertRaises(ValueError):
            main._parse_json_objeto("[]")
        with self.assertRaises(ValueError):
            main._parse_json_objeto("")


class Extraccion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.conversation_state.clear()

    def tearDown(self):
        main.conversation_state.clear()

    async def test_reintenta_una_vez_si_falla_y_usa_el_segundo_intento(self):
        llamadas = {"n": 0}

        def solicitar(_contenido):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                raise TimeoutError("se cortó")
            return json.dumps(_datos_horario())

        with patch("main._solicitar_extraccion_envasado", solicitar), patch(
            "main.ENVASADO_REINTENTO_ESPERA", 0
        ):
            data = await main.extraer_datos_envasado_vasitos(
                {"6": "José Luis y ángel de 6:15 a 9:30", "7": "673"}
            )
        self.assertEqual(llamadas["n"], 2)
        self.assertEqual(data["vasitos"], 673)

    async def test_reintenta_si_el_json_viene_incompleto(self):
        llamadas = {"n": 0}

        def solicitar(_contenido):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                return json.dumps({"hubo_envasado": True, "vasitos": 673})
            return json.dumps(_datos_horario())

        with patch("main._solicitar_extraccion_envasado", solicitar), patch(
            "main.ENVASADO_REINTENTO_ESPERA", 0
        ):
            data = await main.extraer_datos_envasado_vasitos({"6": "de 6:15 a 9:30", "7": "673"})
        self.assertEqual(llamadas["n"], 2)
        self.assertTrue(main._tiene_tramo_util(data))

    async def test_no_reintenta_si_el_primero_alcanza(self):
        llamadas = {"n": 0}

        def solicitar(_contenido):
            llamadas["n"] += 1
            return json.dumps(_datos_horario())

        with patch("main._solicitar_extraccion_envasado", solicitar):
            await main.extraer_datos_envasado_vasitos({"6": "de 6:15 a 9:30", "7": "673"})
        self.assertEqual(llamadas["n"], 1)

    async def test_dos_fallos_dejan_vacio_y_el_canal_avisa_a_tania(self):
        llamadas = {"n": 0}

        def solicitar(_contenido):
            llamadas["n"] += 1
            raise TimeoutError("timeout")

        state = {
            "answers": {
                "6": "José Luis y ángel de 6:15 a 9:30",
                "7": "673",
                "8": "2, José Luis y Ángel",
                "9": "0",
            },
            "vasitos_n_personas": 2,
            "vasitos_pausa_minutos": 0,
            "vasitos_personas_detalle": "José Luis y Ángel",
        }
        with patch("main._solicitar_extraccion_envasado", solicitar), patch(
            "main.ENVASADO_REINTENTO_ESPERA", 0
        ):
            texto = await main.mensaje_calculo_envasado(state)
        self.assertEqual(llamadas["n"], 2)
        self.assertIn("No pude calcular la meta de envasado de vasitos", texto)
        self.assertIn("Tania", texto)
        self.assertNotIn("s/vasito", texto)
        self.assertFalse(state["calculo_envasado"]["ok"])

    async def test_mensaje_ignora_el_conteo_de_grok_aunque_no_esten_los_campos_de_estado(self):
        def solicitar(_contenido):
            datos = _datos_horario(vasitos=999)
            return json.dumps(datos)

        state = {
            "answers": {
                "6": "José Luis y ángel de 6:15 a 9:30",
                "7": "673",
                "8": "2, José Luis y Ángel",
                "9": "15 minutos",
            }
        }
        with patch("main._solicitar_extraccion_envasado", solicitar):
            texto = await main.mensaje_calculo_envasado(state)
        self.assertIn("673", texto)
        self.assertNotIn("999", texto)
        self.assertIn("Meta (2 personas): **26 s/vasito**", texto)
        self.assertNotIn("18 s/vasito", texto)
        self.assertIn("Pausa descontada: 15 min", texto)
        self.assertIn("Tiempo bruto: 3 h 15 min (11700 s)", texto)
        self.assertIn("Tiempo neto: 3 h (10800 s)", texto)
        self.assertIn("Tania", texto)
        self.assertEqual(state["calculo_envasado"]["n_trabajadores"], 2)
        self.assertEqual(state["calculo_envasado"]["pausa_minutos"], 15)
        self.assertEqual(state["calculo_envasado"]["vasitos"], 673)

    async def test_cero_vasitos_no_llama_a_grok_ni_nombra_a_tania(self):
        def solicitar(_contenido):
            raise AssertionError("no debía llamar a Grok")

        state = {"answers": {"7": "0"}}
        with patch("main._solicitar_extraccion_envasado", solicitar):
            texto = await main.mensaje_calculo_envasado(state)
        self.assertEqual(texto, main.MENSAJE_SIN_VASITOS)
        self.assertNotIn("Tania", texto)


class Flujo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.conversation_state.clear()
        self.canal = _Canal()

    def tearDown(self):
        main.conversation_state.clear()

    def _poner(self, state):
        main.conversation_state[str(self.canal.id)] = state
        return state

    async def test_flujo_del_30_de_octubre(self):
        state = self._poner(
            _estado(
                main.IDX_VASITOS_CANTIDAD,
                answers={"6": "José Luis y ángel de 6:15 a 9:30"},
            )
        )

        def solicitar(_contenido):
            return json.dumps(_datos_horario(vasitos=999, n_grok=3, pausa_tramo=40))

        with patch("main.consultar_grok", _grok_valido), patch(
            "main._solicitar_extraccion_envasado", solicitar
        ):
            await main.manejar_respuesta(_Msg("673", self.canal))
            self.assertEqual(state["paso"], main.IDX_VASITOS_PERSONAS)
            self.assertEqual(state["answers"]["7"], "673")
            self.assertNotIn("calculo_envasado", state)
            self.assertIn("Cuántas personas", self.canal.mensajes[-1])

            await main.manejar_respuesta(_Msg("José Luis y ángel", self.canal))
            self.assertEqual(state["paso"], main.IDX_VASITOS_PERSONAS)
            self.assertNotIn("8", state["answers"])
            self.assertEqual(state["buffer"], "José Luis y ángel")
            self.assertIn("número de personas", self.canal.mensajes[-1])

            await main.manejar_respuesta(_Msg(".", self.canal))
            self.assertEqual(state["buffer"], "José Luis y ángel")
            self.assertEqual(state["paso"], main.IDX_VASITOS_PERSONAS)

            await main.manejar_respuesta(_Msg("2", self.canal))
            self.assertEqual(state["vasitos_n_personas"], 2)
            self.assertIn("ángel", state["vasitos_personas_detalle"])
            self.assertEqual(state["buffer"], "")
            self.assertEqual(state["paso"], main.IDX_VASITOS_PAUSAS)
            self.assertIn("minutos pausaron", self.canal.mensajes[-1])

            await main.manejar_respuesta(_Msg("0", self.canal))

        texto = self.canal.mensajes[-2]
        self.assertIn("Cálculo de envasado de vasitos", texto)
        self.assertIn("Tiempo bruto: 3 h 15 min (11700 s)", texto)
        self.assertIn("Pausa descontada: 0 min", texto)
        self.assertIn("Tiempo neto: 3 h 15 min (11700 s)", texto)
        self.assertIn("673", texto)
        self.assertIn("17,4 s/vasito", texto)
        self.assertIn("Meta (2 personas): **26 s/vasito**", texto)
        self.assertNotIn("18 s/vasito", texto)
        self.assertNotIn("Tania", texto)
        self.assertTrue(
            self.canal.mensajes[-1].startswith("9. ¿A qué hora empezaste y terminaste la limpieza?")
        )
        self.assertEqual(state["paso"], main.IDX_VASITOS_PAUSAS + 1)
        self.assertEqual(state["answers"]["6"], "José Luis y ángel de 6:15 a 9:30")
        self.assertTrue(state["calculo_envasado"]["ok"])
        self.assertEqual(state["calculo_envasado"]["n_trabajadores"], 2)
        self.assertEqual(state["calculo_envasado"]["pausa_minutos"], 0)

    async def test_si_grok_falla_igual_avisa_y_sigue_el_reporte(self):
        state = self._poner(
            _estado(
                main.IDX_VASITOS_PAUSAS,
                answers={
                    "6": "José Luis y ángel de 6:15 a 9:30",
                    "7": "673",
                    "8": "2, José Luis y Ángel",
                },
                vasitos_n_personas=2,
                vasitos_personas_detalle="José Luis y Ángel",
            )
        )

        def solicitar(_contenido):
            raise TimeoutError("timeout")

        with patch("main._solicitar_extraccion_envasado", solicitar), patch(
            "main.ENVASADO_REINTENTO_ESPERA", 0
        ):
            await main.manejar_respuesta(_Msg("0", self.canal))
        self.assertIn("Tania", self.canal.mensajes[-2])
        self.assertIn("No pude calcular la meta", self.canal.mensajes[-2])
        self.assertTrue(self.canal.mensajes[-1].startswith("9. "))
        self.assertEqual(state["paso"], main.IDX_VASITOS_PAUSAS + 1)
        self.assertFalse(state["calculo_envasado"]["ok"])

    async def test_cero_vasitos_salta_personas_y_pausa(self):
        state = self._poner(_estado(main.IDX_VASITOS_CANTIDAD, answers={"6": "no se envasaron"}))

        def solicitar(_contenido):
            raise AssertionError("no debía llamar a Grok")

        with patch("main.consultar_grok", _grok_valido), patch(
            "main._solicitar_extraccion_envasado", solicitar
        ):
            await main.manejar_respuesta(_Msg("no se envasaron vasitos", self.canal))
        self.assertEqual(state["paso"], main.IDX_VASITOS_PAUSAS + 1)
        self.assertIn("No se envasaron vasitos hoy", self.canal.mensajes[-2])
        self.assertNotIn("Tania", self.canal.mensajes[-2])
        self.assertTrue(self.canal.mensajes[-1].startswith("9. "))
        self.assertNotIn("8", state["answers"])

    async def test_reanuda_limpieza_sin_perder_respuestas(self):
        idx = main.IDX_VASITOS_PAUSAS + 1
        state = self._poner(
            _estado(idx, answers={"0": "Angel y Luis", "7": "673"})
        )
        with patch("main.consultar_grok", _grok_valido):
            await main.manejar_respuesta(_Msg("Yuselis de 5:45pm a 6:45pm", self.canal))
        self.assertEqual(state["answers"][str(idx)], "Yuselis de 5:45pm a 6:45pm")
        self.assertEqual(state["answers"]["7"], "673")
        self.assertEqual(state["paso"], idx + 1)
        self.assertIn("tambores", self.canal.mensajes[-1])

    async def test_despues_de_limpiar_los_tambores_la_foto_es_obligatoria(self):
        idx = main.IDX_BATIDORAS_FOTO - 1
        state = self._poner(_estado(idx))

        async def _grok_no_entra(*_args, **_kwargs):
            raise AssertionError("la foto de las batidoras no se valida con Grok")

        with patch("main.consultar_grok", _grok_valido):
            await main.manejar_respuesta(_Msg("sí, limpios y sin sal", self.canal))
        self.assertEqual(state["paso"], main.IDX_BATIDORAS_FOTO)
        self.assertEqual(state["answers"][str(idx)], "sí, limpios y sin sal")
        self.assertEqual(self.canal.mensajes[-1], main.TEXTO_BATIDORAS_FOTO)

        with patch("main.consultar_grok", _grok_no_entra):
            await main.manejar_respuesta(_Msg("sí", self.canal))
            self.assertEqual(state["paso"], main.IDX_BATIDORAS_FOTO)
            self.assertNotIn(str(main.IDX_BATIDORAS_FOTO), state["answers"])
            self.assertEqual(state["buffer"], "sí")
            self.assertIn("Falta la foto", self.canal.mensajes[-1])
            self.assertIn(main.TEXTO_BATIDORAS_FOTO, self.canal.mensajes[-1])

            await main.manejar_respuesta(_Msg(".", self.canal))
            self.assertEqual(state["paso"], main.IDX_BATIDORAS_FOTO)
            self.assertEqual(state["buffer"], "sí")

            msg = _Msg("", self.canal, attachments=[_Adjunto("batidoras.jpg")])
            await main.manejar_respuesta(msg)

        guardado = state["answers"][str(main.IDX_BATIDORAS_FOTO)]
        self.assertEqual(guardado, "sí\n[Adjuntos: batidoras.jpg]")
        self.assertEqual(state["buffer"], "")
        self.assertEqual(state["paso"], main.IDX_PLANTA_AZUL_PRENDIDA)
        self.assertEqual(self.canal.mensajes[-1], main.TEXTO_PLANTA_AZUL_PRENDIDA)
        self.assertEqual(msg.reactions, ["✅"])

    async def test_varias_fotos_de_las_batidoras_en_el_mismo_mensaje(self):
        state = self._poner(_estado(main.IDX_BATIDORAS_FOTO))
        fotos = [_Adjunto(f"batidora-{n}.jpg") for n in (1, 2, 3, 4)]

        async def _grok_no_entra(*_args, **_kwargs):
            raise AssertionError("la foto de las batidoras no se valida con Grok")

        with patch("main.consultar_grok", _grok_no_entra):
            await main.manejar_respuesta(_Msg("las cuatro", self.canal, attachments=fotos))

        guardado = state["answers"][str(main.IDX_BATIDORAS_FOTO)]
        self.assertEqual(
            guardado,
            "las cuatro\n[Adjuntos: batidora-1.jpg, batidora-2.jpg, batidora-3.jpg, batidora-4.jpg]",
        )
        self.assertEqual(state["paso"], main.IDX_PLANTA_AZUL_PRENDIDA)
        self.assertEqual(self.canal.mensajes[-1], main.TEXTO_PLANTA_AZUL_PRENDIDA)

    async def test_la_foto_del_gasoil_sigue_exigiendo_el_adjunto(self):
        state = self._poner(
            _estado(main.IDX_GASOIL_AZUL_FOTO_NIVEL, gasoil_azul_lineas=5)
        )

        async def _grok_no_entra(*_args, **_kwargs):
            raise AssertionError("la foto del gasoil no se valida con Grok")

        with patch("main.consultar_grok", _grok_no_entra):
            await main.manejar_respuesta(_Msg("ahí está", self.canal))
            self.assertEqual(state["paso"], main.IDX_GASOIL_AZUL_FOTO_NIVEL)
            self.assertIn("Falta la foto", self.canal.mensajes[-1])
            self.assertEqual(state["buffer"], "ahí está")

            await main.manejar_respuesta(
                _Msg("", self.canal, attachments=[_Adjunto("nivel.jpg")])
            )

        self.assertEqual(
            state["answers"][str(main.IDX_GASOIL_AZUL_FOTO_NIVEL)],
            "ahí está\n[Adjuntos: nivel.jpg]",
        )
        self.assertEqual(state["paso"], main.IDX_ACEITE_AZUL)
        self.assertTrue(self.canal.mensajes[-1].startswith("11. ¿Cuál es el nivel de aceite"))
        self.assertNotIn("Debes colocarle gasoil", "\n".join(self.canal.mensajes))

    async def test_planta_azul_sigue_bifurcando(self):
        state = self._poner(_estado(main.IDX_PLANTA_AZUL_PRENDIDA))
        await main.manejar_respuesta(_Msg("sí", self.canal))
        self.assertTrue(state["planta_azul_prendida"])
        self.assertEqual(state["paso"], main.IDX_GASOIL_AZUL_LINEAS)
        self.assertIn("antes de prenderla", self.canal.mensajes[-1])

        await main.manejar_respuesta(_Msg("4", self.canal))
        self.assertEqual(state["paso"], main.IDX_PLANTA_AZUL_SWITCH + 1)
        self.assertTrue(self.canal.mensajes[-1].startswith("12. ¿Cuánto gasoil"))
        self.assertNotIn(str(main.IDX_GASOIL_AZUL_FOTO_NIVEL), state["answers"])

    async def test_planta_apagada_pide_la_foto(self):
        state = self._poner(_estado(main.IDX_PLANTA_AZUL_PRENDIDA))
        await main.manejar_respuesta(_Msg("apagada", self.canal))
        self.assertFalse(state["planta_azul_prendida"])
        self.assertEqual(state["paso"], main.IDX_GASOIL_AZUL_LINEAS)
        self.assertEqual(self.canal.mensajes[-1], main.TEXTO_GASOIL_AZUL_LINEAS)

        await main.manejar_respuesta(_Msg("3,5", self.canal))
        self.assertEqual(state["paso"], main.IDX_GASOIL_AZUL_FOTO_NIVEL)
        self.assertIn("foto del nivel de gasoil", self.canal.mensajes[-1])
        self.assertEqual(state["answers"][str(main.IDX_GASOIL_AZUL_LINEAS)], "3.5")

    async def test_cancelar_un_reporte_en_la_pregunta_de_personas(self):
        self._poner(_estado(main.IDX_VASITOS_PERSONAS, answers={"7": "673"}))
        await main.on_message(_Msg("cancelar", self.canal))
        self.assertNotIn(str(self.canal.id), main.conversation_state)
        self.assertIn("cancelado", self.canal.mensajes[-1])


if __name__ == "__main__":
    unittest.main()
