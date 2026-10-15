"""Adaptador de almacenamiento de objetos JSON sobre Amazon S3 (``ObjectStorage``).

Este módulo implementa el puerto :class:`sentiment.domain.ports.ObjectStorage`
sobre el Bucket_Analisis del Sistema. Es el único punto por el que el núcleo de
la aplicación lee y escribe los documentos JSON del pipeline bajo los tres
prefijos del layout de S3 (diseño 6.5):

* ``inputs/{tenant_id}/{yyyy}/{mm}/{dd}/{analysis_id}.json`` — el payload
  canonicalizado del Lote que escribe Api_Submit (criterio 4.1).
* ``outputs/{tenant_id}/{yyyy}/{mm}/{dd}/{analysis_id}.json`` — el
  Documento_Consolidado que lee y descarga Api_Download (Requirement 12).
* ``map-results/{analysis_id}/`` — el artefacto del ``ResultWriter`` del
  Distributed_Map.

El adaptador cumple la frontera hexagonal (diseño 2.1): recibe el cliente de
boto3 por inyección (``s3_client`` de :mod:`sentiment.adapters.aws_clients`) y
no construye clientes propios, de modo que hereda la política de **cero
reintentos del SDK** (criterio 9.18) y la reutilización de conexiones entre
invocaciones calientes (criterio 17.11). La política de reintentos vive en Step
Functions, no aquí.

Decisiones de diseño relevantes:

* **Nombre del bucket desde el entorno (criterio 11.1).** El nombre
  ``sentiment-{env}-analysis-{account}`` lo inyecta la Infraestructura como la
  variable de entorno ``BUCKET_NAME``. Se resuelve de forma **perezosa y
  memoizada** (nunca a nivel de módulo), para que importar este módulo no
  dependa del entorno y preserve el recorrido de importabilidad del paquete
  (criterio 17.7). Un ``BUCKET_NAME`` ausente o en blanco es un
  :class:`~sentiment.observability.config.ConfigError` accionable, no un
  ``KeyError`` opaco.

* **Cifrado en reposo con la CMK (criterios 12.2, 16.4).** El bucket aplica
  ``SSE-KMS`` con ``BucketKeyEnabled`` por configuración (``storage_stack``), de
  modo que ``PutObject`` cifra con la CMK sin que el adaptador tenga que
  especificar la clave en cada escritura. Las Presigned_URL de descarga
  funcionan con objetos cifrados con KMS porque S3 descifra del lado del
  servidor al servir el ``GetObject`` firmado; el principal que firma la URL
  debe tener permiso ``kms:Decrypt`` sobre la CMK, lo que garantiza la
  Politica_IAM de ``api_download``.

* **Presigned_URL acotada a un único objeto (criterios 12.2, 10.3).**
  :meth:`S3Storage.presign_get` firma exclusivamente un ``get_object`` sobre la
  clave exacta recibida, nunca un prefijo ni el bucket, con una validez por
  defecto de 900 segundos. La URL se genera bajo demanda y **nunca** se persiste
  ni se registra en logs (criterio 10.3); este módulo no la escribe en ningún
  log.

* **Sin PII en logs (criterios 15.1, 15.2).** Los documentos JSON de
  ``inputs/`` y ``outputs/`` contienen el texto de ``respuesta`` y de
  ``explicacion``, que son PII. El adaptador registra únicamente metadatos no
  sensibles —la clave del objeto, el tamaño en bytes y el tipo de error—, nunca
  el contenido del objeto ni la Presigned_URL emitida.

* **Purga: borrado de todas las versiones (``PurgeStorage``, criterio 19.4).**
  El mismo adaptador implementa el puerto
  :class:`~sentiment.domain.ports.PurgeStorage` mediante
  :meth:`S3Storage.delete_all_versions`. Como el bucket tiene el **versionado
  activado** (criterio 16.7), un ``DeleteObject`` simple solo pondría un marcador
  de eliminación y dejaría las versiones anteriores recuperables; por eso la
  Purga enumera con ``ListObjectVersions`` y borra con ``DeleteObjects``
  especificando el ``VersionId`` de **cada** versión y de **cada** marcador de
  eliminación, tanto de las claves exactas como de todo lo que cuelga de un
  prefijo (diseño 12.8). La operación es **reintentable** (criterio 19.8): una
  segunda llamada sin versiones que borrar devuelve ``0`` sin error, lo que
  permite al cliente repetir el ``DELETE`` tras un fallo parcial.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Final

from botocore.exceptions import ClientError

from sentiment.adapters.aws_clients import s3_client
from sentiment.domain.errors import NotFoundError
from sentiment.observability.config import ConfigError
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from botocore.client import BaseClient

__all__ = ["S3Storage", "DEFAULT_PRESIGN_EXPIRY_SECONDS"]

logger = get_logger(service="s3_storage")

#: Nombre de la variable de entorno con el nombre del Bucket_Analisis, inyectada
#: por la Infraestructura con la forma ``sentiment-{env}-analysis-{account}``
#: (criterio 11.1).
_ENV_BUCKET_NAME: Final = "BUCKET_NAME"

#: Validez por defecto de una Presigned_URL de descarga, en segundos. Es el
#: mínimo razonable para una descarga humana y el valor exacto que exige el
#: criterio 12.2 (900 s = 15 min). El camino de descarga (Api_Download) usa este
#: valor; se expone como constante para que no se duplique como literal mágico.
DEFAULT_PRESIGN_EXPIRY_SECONDS: Final = 900

#: Número máximo de objetos que admite una sola petición ``DeleteObjects`` de S3
#: (límite duro de la API). La Purga borra las versiones en tandas de este tamaño.
_DELETE_OBJECTS_BATCH_SIZE: Final = 1000

#: Códigos de error de S3/botocore que denotan inequívocamente "el objeto no
#: existe" en una respuesta a ``GetObject``. ``NoSuchKey`` es el código canónico
#: de la API; ``404`` aparece cuando el cliente no tiene permiso de
#: ``s3:ListBucket`` y S3 devuelve un ``404`` genérico para no revelar la
#: existencia del objeto. Ambos se traducen al error de "no encontrado" del
#: dominio.
_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"NoSuchKey", "404"})


def _bucket_name() -> str:
    """Resuelve el nombre del Bucket_Analisis desde el entorno (criterio 11.1).

    La lectura es perezosa (no a nivel de módulo) para no acoplar la importación
    del paquete al entorno de ejecución (criterio 17.7). No se memoiza en este
    módulo porque la resolución es trivial; la variable de entorno es estable
    durante toda la vida del contenedor de la Lambda.

    :return: el nombre del bucket, no vacío y recortado.
    :raises ConfigError: si ``BUCKET_NAME`` falta, está vacío o es solo espacios.
    """
    raw = os.environ.get(_ENV_BUCKET_NAME)
    if raw is None or not raw.strip():
        raise ConfigError(
            f"Falta la configuración requerida '{_ENV_BUCKET_NAME}'. Debe "
            f"inyectarse como variable de entorno con el nombre del Bucket_Analisis "
            f"('sentiment-{{env}}-analysis-{{account}}')."
        )
    return raw.strip()


class S3Storage:
    """Implementación de :class:`~sentiment.domain.ports.ObjectStorage` sobre S3.

    Lee y escribe documentos JSON del pipeline en el Bucket_Analisis y emite las
    Presigned_URL de descarga. El cliente de S3 se inyecta en el constructor
    (inversión de dependencias): por defecto se usa el singleton compartido de
    :func:`sentiment.adapters.aws_clients.s3_client`, reutilizado entre
    invocaciones calientes y sin reintentos del SDK (criterios 17.11, 9.18).

    No construye su propio cliente ni resuelve el nombre del bucket en el
    constructor: ambos se resuelven de forma perezosa, lo que mantiene el módulo
    importable sin entorno ni acceso a AWS (criterio 17.7) y permite inyectar un
    doble de prueba en los tests de contrato.
    """

    def __init__(
        self, client: BaseClient | None = None, bucket_name: str | None = None
    ) -> None:
        """Inicializa el adaptador con el cliente de S3 y el bucket objetivo.

        :param client: cliente de boto3 de S3 a usar; si es ``None`` se toma el
            singleton compartido de :func:`aws_clients.s3_client`.
        :param bucket_name: nombre del bucket; si es ``None`` se resuelve de forma
            perezosa desde ``BUCKET_NAME`` en la primera operación (criterio 11.1).
        """
        self._client = client if client is not None else s3_client()
        self._bucket_name = bucket_name

    def _bucket(self) -> str:
        """Devuelve el nombre del bucket, resolviéndolo del entorno si hace falta.

        :return: el nombre del Bucket_Analisis.
        :raises ConfigError: si no se inyectó un nombre y ``BUCKET_NAME`` no está
            disponible.
        """
        if self._bucket_name is None:
            self._bucket_name = _bucket_name()
        return self._bucket_name

    def put_json(self, key: str, payload: object) -> None:
        """Serializa ``payload`` a JSON (UTF-8) y lo escribe en el objeto ``key``.

        La serialización usa ``ensure_ascii=False`` para preservar los caracteres
        no ASCII (acentos, ñ, emojis) de las respuestas en español sin inflarlos a
        secuencias ``\\uXXXX``, y codifica el resultado en UTF-8. El objeto queda
        cifrado en reposo con la CMK por la configuración ``SSE-KMS`` del bucket;
        el adaptador no necesita especificar la clave en la petición (diseño 12.3).

        La escritura es atómica a ojos de S3: ``PutObject`` o deja el objeto
        completo o falla sin dejar un objeto parcial (postcondición del puerto).

        :param key: clave del objeto, bajo uno de los prefijos ``inputs/``,
            ``outputs/`` o ``map-results/`` (criterios 4.1, 10.3).
        :param payload: estructura serializable a JSON (dict, list, escalar).
        :raises ClientError: si la escritura en S3 falla; el llamante decide la
            traducción (p. ej. ``INTERNAL_ERROR`` HTTP 500 en Api_Submit,
            criterio 4.10). No se registra el contenido del objeto (PII).
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            # `boto3-stubs` se instala sin los *extras* por servicio (ver
            # `aws_clients.py`), por lo que `BaseClient` no expone los métodos
            # específicos de S3 al verificador estático; se silencia solo el
            # acceso al atributo, no la verificación de los argumentos.
            self._client.put_object(  # type: ignore[attr-defined]
                Bucket=self._bucket(),
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except ClientError as exc:
            # Solo metadatos no sensibles: clave, tamaño y tipo de error. Nunca el
            # contenido del objeto, que es PII (criterios 15.1, 15.2).
            logger.warning(
                "s3_put_json_failed",
                extra={
                    "s3_key": key,
                    "body_bytes": len(body),
                    "error_type": exc.__class__.__name__,
                },
            )
            raise
        logger.info(
            "s3_put_json_succeeded",
            extra={"s3_key": key, "body_bytes": len(body)},
        )

    def get_json(self, key: str) -> object:
        """Lee el objeto ``key`` y lo deserializa desde JSON.

        :param key: clave exacta del objeto a leer.
        :return: el objeto Python deserializado del JSON almacenado.
        :raises NotFoundError: si el objeto no existe en el bucket (``NoSuchKey``
            o ``404``). El llamante decide la traducción (p. ej. HTTP 404 en
            Api_Download, criterio 12.5).
        :raises ClientError: ante cualquier otro fallo de S3 distinto de "no
            encontrado"; se propaga sin registrar el contenido del objeto (PII).
        """
        try:
            # Ver la nota de tipado de `put_json`: `BaseClient` no expone
            # `get_object` sin los *extras* de `boto3-stubs`.
            response = self._client.get_object(  # type: ignore[attr-defined]
                Bucket=self._bucket(), Key=key
            )
            body = response["Body"].read()
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _NOT_FOUND_CODES:
                logger.info("s3_get_json_not_found", extra={"s3_key": key})
                raise NotFoundError(s3_key=key) from exc
            logger.warning(
                "s3_get_json_failed",
                extra={"s3_key": key, "error_type": exc.__class__.__name__},
            )
            raise
        logger.info(
            "s3_get_json_succeeded",
            extra={"s3_key": key, "body_bytes": len(body)},
        )
        # El contenido deserializado NUNCA se registra: puede contener PII.
        return json.loads(body.decode("utf-8"))

    def object_exists(self, key: str) -> bool:
        """Comprueba la existencia de un objeto con ``HeadObject`` (sin descargar el cuerpo).

        Implementa el puerto :class:`~sentiment.domain.ports.ObjectStorage`.
        Usa ``head_object`` en lugar de ``get_object`` para no transferir ni
        materializar el contenido del Documento_Consolidado —que es PII— cuando
        Api_Download solo necesita saber si el objeto referenciado por el
        ``output_s3_key`` del Lote sigue existiendo (criterio 12.5), p. ej. tras
        su expiración por el ciclo de vida.

        :param key: clave exacta del objeto a comprobar.
        :return: ``True`` si el objeto existe; ``False`` si S3 responde "no
            encontrado" (``NoSuchKey``/``404``).
        :raises ClientError: ante cualquier otro fallo de S3 distinto de "no
            encontrado" (red o permisos); el llamante lo trata como fallo interno
            (HTTP 500), nunca como ausencia del objeto.
        """
        try:
            # Ver la nota de tipado de `put_json`: `BaseClient` no expone
            # `head_object` sin los *extras* de `boto3-stubs`.
            self._client.head_object(  # type: ignore[attr-defined]
                Bucket=self._bucket(), Key=key
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _NOT_FOUND_CODES:
                logger.info("s3_object_exists_absent", extra={"s3_key": key})
                return False
            logger.warning(
                "s3_object_exists_failed",
                extra={"s3_key": key, "error_type": exc.__class__.__name__},
            )
            raise
        logger.info("s3_object_exists_present", extra={"s3_key": key})
        return True

    def presign_get(
        self, key: str, expires_in: int = DEFAULT_PRESIGN_EXPIRY_SECONDS
    ) -> str:
        """Genera una Presigned_URL de solo lectura (GET) acotada a un único objeto.

        La URL firma exclusivamente un ``get_object`` sobre la clave exacta
        ``key`` —nunca un prefijo ni el bucket (criterio 12.2)— y caduca pasados
        ``expires_in`` segundos (por defecto 900 s, el valor exacto del criterio
        12.2). El objeto está cifrado con la CMK; la URL firmada funciona porque
        S3 descifra del lado del servidor al servir la descarga, siempre que el
        principal que firmó tenga ``kms:Decrypt`` sobre la CMK (lo garantiza la
        Politica_IAM de ``api_download``).

        La URL se genera bajo demanda y **no** se persiste ni se registra en
        ningún log (criterio 10.3); este método no la escribe en el logger. Solo
        se registran la clave y la validez como metadatos no sensibles.

        :param key: clave exacta del objeto a descargar (un único objeto).
        :param expires_in: validez de la URL en segundos; el camino de descarga
            usa 900 (criterio 12.2).
        :return: una URL HTTPS firmada de solo lectura para ``key``, válida
            durante ``expires_in`` segundos.
        :raises ClientError: si la generación de la URL falla en el cliente de S3.
        """
        try:
            # Ver la nota de tipado de `put_json`: sin los *extras* de
            # `boto3-stubs`, `BaseClient` no expone `generate_presigned_url`. Se
            # asigna a una variable anotada para fijar el tipo de retorno `str`.
            url: str = self._client.generate_presigned_url(  # type: ignore[attr-defined]
                ClientMethod="get_object",
                Params={"Bucket": self._bucket(), "Key": key},
                ExpiresIn=expires_in,
            )
        except ClientError as exc:
            logger.warning(
                "s3_presign_get_failed",
                extra={"s3_key": key, "error_type": exc.__class__.__name__},
            )
            raise
        # Nunca se registra la URL firmada (es un secreto portador, criterio 10.3).
        logger.info(
            "s3_presign_get_issued",
            extra={"s3_key": key, "expires_in": expires_in},
        )
        return url

    def delete_all_versions(self, exact_keys: list[str], prefix: str) -> int:
        """Borra todas las versiones de ``exact_keys`` y de todo lo que cuelga de ``prefix``.

        Implementa el puerto :class:`~sentiment.domain.ports.PurgeStorage`. Como
        el bucket tiene el versionado activado (criterio 16.7), enumera con
        ``ListObjectVersions`` (paginado) y borra con ``DeleteObjects``
        especificando el ``VersionId`` de **cada** versión y de **cada** marcador
        de eliminación. Esto garantiza que, tras retornar, no quede ningún dato
        recuperable en S3 de ese Lote (criterio 19.4), ni siquiera un marcador de
        eliminación que un ``DeleteObject`` simple habría dejado atrás.

        La enumeración abarca dos conjuntos y los deduplica por
        ``(Key, VersionId)``: (1) las ``exact_keys`` (los objetos ``inputs/`` y
        ``outputs/`` del Lote), listadas con ``KeyMarker`` acotado a cada clave; y
        (2) todo lo que cuelga de ``prefix`` (``map-results/{analysis_id}/``),
        listado con ``Prefix``. Una clave exacta que además cayera bajo el prefijo
        se borra una sola vez.

        La operación es **reintentable** (criterio 19.8): una segunda llamada,
        cuando ya no quedan versiones, enumera un conjunto vacío y devuelve ``0``
        sin error, de modo que el cliente puede repetir el ``DELETE`` tras un
        fallo parcial sin efectos adversos.

        :param exact_keys: claves exactas a eliminar por completo (todas sus
            versiones); típicamente las claves de ``inputs/`` y ``outputs/`` del
            Lote. Las entradas vacías o en blanco se ignoran.
        :param prefix: prefijo bajo el cual se eliminan todos los objetos y sus
            versiones; típicamente ``map-results/{analysis_id}/``. Una cadena
            vacía se ignora (no se enumera el bucket completo), para no borrar
            fuera del alcance del Lote.
        :return: el número total de versiones y marcadores de eliminación
            efectivamente eliminados.
        :raises ClientError: si la enumeración o el borrado en S3 falla; el
            llamante (``purge_analysis``) lo traduce a ``INTERNAL_ERROR`` (HTTP
            500) y conserva el META para que la Purga sea reintentable
            (criterio 19.7). No se registra el contenido de ningún objeto (PII).

        Postcondiciones:
            - Tras retornar, no queda **ninguna** versión ni marcador de
              eliminación de las ``exact_keys`` ni bajo ``prefix`` (criterio 19.4).
            - Es reintentable: una segunda llamada sin versiones devuelve ``0``.
        """
        bucket = self._bucket()
        # Deduplicación por (Key, VersionId): una clave exacta que además caiga
        # bajo el prefijo no debe intentar borrarse dos veces.
        targets: dict[tuple[str, str], None] = {}

        for key in exact_keys:
            if key and key.strip():
                for version in self._iter_versions(bucket, key_prefix=key):
                    targets[version] = None
        if prefix and prefix.strip():
            for version in self._iter_versions(bucket, key_prefix=prefix):
                targets[version] = None

        if not targets:
            logger.info(
                "s3_delete_all_versions_noop",
                extra={"prefix": prefix, "exact_key_count": len(exact_keys)},
            )
            return 0

        deleted = 0
        version_list = list(targets.keys())
        for start in range(0, len(version_list), _DELETE_OBJECTS_BATCH_SIZE):
            batch = version_list[start : start + _DELETE_OBJECTS_BATCH_SIZE]
            deleted += self._delete_versions_batch(bucket, batch)

        logger.info(
            "s3_delete_all_versions_succeeded",
            extra={
                "prefix": prefix,
                "exact_key_count": len(exact_keys),
                "versions_deleted": deleted,
            },
        )
        return deleted

    def _iter_versions(
        self, bucket: str, key_prefix: str
    ) -> list[tuple[str, str]]:
        """Enumera todas las versiones y marcadores bajo ``key_prefix``.

        Pagina ``ListObjectVersions`` con ``Prefix=key_prefix`` y reúne tanto las
        ``Versions`` como los ``DeleteMarkers`` (ambos deben borrarse para no
        dejar datos recuperables, criterio 19.4). Se usa el mismo método tanto
        para una clave exacta como para un prefijo: con una clave exacta, el
        prefijo coincide con la clave y S3 puede devolver además objetos cuyo
        nombre empiece por esa clave; por eso el llamante filtra por igualdad
        cuando corresponde. En este Sistema las claves son ULID únicos, por lo que
        el prefijo de una clave exacta no colisiona con otras claves del Lote.

        :param bucket: nombre del Bucket_Analisis.
        :param key_prefix: clave exacta o prefijo a enumerar.
        :return: lista de pares ``(Key, VersionId)`` de todas las versiones y
            marcadores encontrados.
        """
        versions: list[tuple[str, str]] = []
        key_marker: str | None = None
        version_id_marker: str | None = None
        while True:
            kwargs: dict[str, object] = {
                "Bucket": bucket,
                "Prefix": key_prefix,
            }
            if key_marker is not None:
                kwargs["KeyMarker"] = key_marker
            if version_id_marker is not None:
                kwargs["VersionIdMarker"] = version_id_marker
            # Ver la nota de tipado de `put_json`: `BaseClient` no expone
            # `list_object_versions` sin los *extras* de `boto3-stubs`.
            response = self._client.list_object_versions(**kwargs)  # type: ignore[attr-defined]

            for entry in response.get("Versions", []):
                versions.append((entry["Key"], entry["VersionId"]))
            for entry in response.get("DeleteMarkers", []):
                versions.append((entry["Key"], entry["VersionId"]))

            if not response.get("IsTruncated"):
                break
            key_marker = response.get("NextKeyMarker")
            version_id_marker = response.get("NextVersionIdMarker")
        return versions

    def _delete_versions_batch(
        self, bucket: str, versions: list[tuple[str, str]]
    ) -> int:
        """Borra una tanda de hasta 1000 versiones con una sola ``DeleteObjects``.

        :param bucket: nombre del Bucket_Analisis.
        :param versions: pares ``(Key, VersionId)`` a eliminar (como máximo 1000).
        :return: el número de versiones que S3 confirmó como eliminadas.
        :raises ClientError: si la petición ``DeleteObjects`` falla por completo.
            Los fallos por objeto se reportan en ``Errors`` sin lanzar excepción;
            se registran como metadatos y se restan del recuento, dejando que la
            reintentabilidad de la Purga complete lo que falte (criterio 19.8).
        """
        objects = [{"Key": key, "VersionId": version_id} for key, version_id in versions]
        # `Quiet=True` evita que la respuesta liste cada objeto borrado (payload
        # innecesario); los errores por objeto sí se devuelven en `Errors`.
        response = self._client.delete_objects(  # type: ignore[attr-defined]
            Bucket=bucket,
            Delete={"Objects": objects, "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            # Solo metadatos: la clave y el código de error de S3, nunca contenido.
            logger.warning(
                "s3_delete_objects_partial_failure",
                extra={"error_count": len(errors)},
            )
        return len(objects) - len(errors)
