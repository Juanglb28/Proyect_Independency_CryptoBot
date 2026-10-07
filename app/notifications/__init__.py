"""Avisos al usuario (Paso 11a). Capa nueva: el envío por Telegram es E/S
externa y no pertenece al dominio ni a la ejecución. Nunca está en el camino de
una orden (P2): el monitor solo deja el aviso en la tabla `alertas`; el envío
lo hace `dispatcher.dispatch_pending_alerts` DESPUÉS de confirmar la
transacción del chequeo."""
