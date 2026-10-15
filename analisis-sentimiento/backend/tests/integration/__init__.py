"""Paquete de pruebas de integración *end-to-end* contra un stack desplegado.

A diferencia del resto de :mod:`tests` —que se ejecutan contra AWS simulado
(``moto``) o en memoria y, por tanto, siempre corren— las pruebas de este
paquete, por diseño, se ejercitan contra un stack **realmente desplegado** en el
entorno ``dev`` (DynamoDB, S3, Step Functions y API Gateway reales). Cuando no
hay stack desplegado ni credenciales AWS, se SALTAN (``skip``) de forma limpia,
nunca fallan: ver :mod:`tests.integration._harness` y
``tests/integration/README.md``.
"""
