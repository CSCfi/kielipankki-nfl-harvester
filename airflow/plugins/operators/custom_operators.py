import os
from more_itertools import peekable

from airflow.models import BaseOperator
from airflow.hooks.base import BaseHook
from airflow.contrib.hooks.ssh_hook import SSHHook
from airflow.models import Connection
from airflow import settings

from harvester.mets import METS
from harvester.file import ALTOFile
from harvester.pmh_interface import PMH_API
from harvester import utils


class CreateConnectionOperator(BaseOperator):
    def __init__(self, conn_id, conn_type, host, schema, **kwargs):
        super().__init__(**kwargs)
        self.conn_id = conn_id
        self.conn_type = conn_type
        self.host = host
        self.schema = schema

    def execute(self, context):
        session = settings.Session()
        conn_ids = [conn.conn_id for conn in session.query(Connection).all()]
        if self.conn_id not in conn_ids:
            conn = Connection(
                conn_id=self.conn_id,
                conn_type=self.conn_type,
                host=self.host,
                schema=self.schema,
            )
            session.add(conn)
            session.commit()


class SaveMetsSFTPOperator(BaseOperator):
    def __init__(self, http_conn_id, ssh_conn_id, dc_identifier, base_path, **kwargs):
        super().__init__(**kwargs)
        self.http_conn_id = http_conn_id
        self.ssh_conn_id = ssh_conn_id
        self.dc_identifier = dc_identifier
        self.base_path = base_path

    def execute(self, context):
        http_conn = BaseHook.get_connection(self.http_conn_id)
        api = PMH_API(url=http_conn.host)

        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()
            output_file = str(
                utils.construct_mets_download_location(
                    dc_identifier=self.dc_identifier,
                    base_path=self.base_path,
                    file_dir="mets",
                )
            )
            utils.make_intermediate_dirs(
                sftp_client=sftp_client,
                remote_directory=output_file.rsplit("/", maxsplit=1)[0],
            )
            with sftp_client.file(output_file, "w") as file:
                api.download_mets(
                    dc_identifier=self.dc_identifier, output_mets_file=file
                )


class SaveMetsForSetSFTPOperator(BaseOperator):
    def __init__(self, http_conn_id, ssh_conn_id, base_path, set_id, **kwargs):
        super().__init__(**kwargs)
        self.http_conn_id = http_conn_id
        self.ssh_conn_id = ssh_conn_id
        self.base_path = base_path
        self.set_id = set_id

    def execute(self, context):
        http_conn = BaseHook.get_connection(self.http_conn_id)
        api = PMH_API(url=http_conn.host)

        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()
            utils.make_intermediate_dirs(
                sftp_client=sftp_client,
                remote_directory=f"{self.base_path}/mets",
            )
            for dc_identifier in api.dc_identifiers(set_id=self.set_id):
                output_file = str(
                    utils.construct_mets_download_location(
                        dc_identifier=dc_identifier,
                        base_path=self.base_path,
                        file_dir="mets",
                    )
                )
                with sftp_client.file(output_file, "w") as file:
                    api.download_mets(
                        dc_identifier=dc_identifier, output_mets_file=file
                    )


class SaveAltosForMetsSFTPOperator(BaseOperator):
    def __init__(self, http_conn_id, ssh_conn_id, base_path, dc_identifier, **kwargs):
        super().__init__(**kwargs)
        self.http_conn_id = http_conn_id
        self.ssh_conn_id = ssh_conn_id
        self.base_path = base_path
        self.dc_identifier = dc_identifier

    def execute(self, context):
        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()

            for file in sftp_client.listdir(f"{self.base_path}/mets"):
                path = os.path.join(f"{self.base_path}/mets", file)
                mets = METS(self.dc_identifier, sftp_client.file(path, "r"))
                alto_files = peekable(mets.files_of_type(ALTOFile))

                first_alto = alto_files.peek()
                first_alto_path = str(
                    utils.construct_file_download_location(
                        file=first_alto, base_path=self.base_path
                    )
                )
                utils.make_intermediate_dirs(
                    sftp_client=sftp_client,
                    remote_directory=first_alto_path.rsplit("/", maxsplit=1)[0],
                )
                for alto_file in alto_files:
                    output_file = str(
                        utils.construct_file_download_location(
                            file=alto_file, base_path=self.base_path
                        )
                    )
                    with sftp_client.file(output_file, "wb") as file:
                        alto_file.download(
                            output_file=file,
                            chunk_size=1024 * 1024,
                        )


class SaveAltosForSetSFTPOperator(BaseOperator):
    def __init__(self, http_conn_id, ssh_conn_id, base_path, set_id, **kwargs):
        super().__init__(**kwargs)
        self.http_conn_id = http_conn_id
        self.ssh_conn_id = ssh_conn_id
        self.base_path = base_path
        self.set_id = set_id

    def execute(self, context):
        http_conn = BaseHook.get_connection(self.http_conn_id)
        api = PMH_API(url=http_conn.host)

        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()

            for dc_identifier in api.dc_identifiers(set_id=self.set_id):
                # NOTE! This expects that all METS files follow the default naming
                # set in utils.construct_mets_download_location. May not be the
                # most optimal solution.
                path = os.path.join(
                    f"{self.base_path}/mets",
                    f"{utils.binding_id_from_dc(dc_identifier)}_METS.xml",
                )
                mets = METS(dc_identifier, sftp_client.file(path, "r"))
                alto_files = peekable(mets.files_of_type(ALTOFile))

                first_alto = alto_files.peek()
                first_alto_path = str(
                    utils.construct_file_download_location(
                        file=first_alto, base_path=self.base_path
                    )
                )
                utils.make_intermediate_dirs(
                    sftp_client=sftp_client,
                    remote_directory=first_alto_path.rsplit("/", maxsplit=1)[0],
                )
                for alto_file in alto_files:
                    output_file = str(
                        utils.construct_file_download_location(
                            file=alto_file, base_path=self.base_path
                        )
                    )
                    with sftp_client.file(output_file, "wb") as file:
                        alto_file.download(
                            output_file=file,
                            chunk_size=10 * 1024 * 1024,
                        )
