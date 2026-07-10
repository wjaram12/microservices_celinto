"""App de Google Workspace (paquete). Reutiliza la infraestructura compartida de
`commons` (API keys) y aporta lo propio: el cliente del Admin SDK Directory API
(usuarios, unidades organizativas y grupos) y la API HTTP que lo expone. Sin caché:
toda consulta va a Google en vivo."""
