"""Pruebas de carga del pipeline contra un stack dev desplegado (tarea 15.4).

Este paquete contiene las pruebas de **carga y concurrencia multi-tenant** que
ejercitan la API desplegada en ``dev`` con 5 Lotes de Tenants distintos de forma
simultánea, verificando la conservación de ítems y midiendo las latencias de
``api_submit``/``api_status`` frente a sus objetivos iniciales.

Las funciones puras de cálculo (percentiles, verificación de conservación de
ítems) viven en :mod:`tests.load._metrics` y se prueban sin red en
:mod:`tests.load.test_load_metrics`. La prueba de carga que toca AWS vive en
:mod:`tests.load.test_load_multitenant` y se **SALTA** limpiamente sin stack.
"""
