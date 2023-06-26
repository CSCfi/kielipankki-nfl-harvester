import os
from requests.exceptions import RequestException

from airflow.models import BaseOperator
from airflow.providers.ssh.hooks.ssh import SSHHook
from airflow.models import Connection
from airflow import settings

from harvester.mets import METS, METSFileEmptyError
from harvester.file import ALTOFile
from harvester import utils


class CreateConnectionOperator(BaseOperator):
    """
    Create any type of Airflow connection.

    :param conn_id: Connection ID
    :param conn_type: Type of connection
    :param host: Host URL
    :param schema: Schema (e.g. HTTPS)
    """

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
            self.log.info(
                "Creating a new %s connection with ID %s", self.conn_type, self.conn_id
            )
            conn = Connection(
                conn_id=self.conn_id,
                conn_type=self.conn_type,
                host=self.host,
                schema=self.schema,
            )
            session.add(conn)
            session.commit()


class SaveFilesSFTPOperator(BaseOperator):
    """
    Save file to a remote filesystem using SSH connection.

    :param sftp_client: SFTPClient
    :param ssh_client: SSHClient
    :param tmpdir: Absolute path for a temporary directory on the remote server
    :param dc_identifier: DC identifier of binding
    :param file_dir: Directory where file will be saved
    """

    def __init__(
        self,
        sftp_client,
        ssh_client,
        tmpdir,
        dc_identifier,
        file_dir,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.sftp_client = sftp_client
        self.ssh_client = ssh_client
        self.dc_identifier = dc_identifier
        self.file_dir = file_dir
        self.tmpdir = tmpdir

    def ensure_tmp_output_location(self):
        """
        Make sure that all intermediate directories exist for temporary storage
        """
        utils.make_intermediate_dirs(
            sftp_client=self.sftp_client, remote_directory=self.tmpdir / self.file_dir
        )

    def execute(self, context):
        raise NotImplementedError(
            "execute() must be defined separately for each file type."
        )

    def file_exists(self, path):
        """
        Check if a non-empty file already exists in the given path.

        :return: True if a non-empty file exists, otherwise False
        """
        try:
            file_size = self.sftp_client.stat(str(path)).st_size
        except OSError:
            return False
        else:
            if file_size > 0:
                return True
        return False


class SaveMetsSFTPOperator(SaveFilesSFTPOperator):
    """
    Save a METS file remote a filesystem using SSH connection.

    :param api: API from which to download the file
    """

    def __init__(self, api, **kwargs):
        super().__init__(**kwargs)
        self.api = api

    def execute(self, context):
        self.ensure_tmp_output_location()

        temp_output_file = str(
            utils.mets_download_location(
                dc_identifier=self.dc_identifier,
                base_path=self.tmpdir,
                file_dir=self.file_dir,
                filename=f"{utils.binding_id_from_dc(self.dc_identifier)}_METS.xml",
            )
        )

        if self.file_exists(temp_output_file):
            return

        with self.sftp_client.file(temp_output_file, "w") as file:
            try:
                self.api.download_mets(
                    dc_identifier=self.dc_identifier, output_mets_file=file
                )
            except RequestException as e:
                raise RequestException(
                    f"METS download {self.dc_identifier} failed: {e.response}"
                )
            except OSError as e:
                raise OSError(
                    f"Writing METS {self.dc_identifier} to file failed with error "
                    f"number {e.errno}"
                )

        if self.sftp_client.stat(temp_output_file).st_size == 0:
            raise METSFileEmptyError(f"METS file {self.dc_identifier} is empty.")


class SaveAltosSFTPOperator(SaveFilesSFTPOperator):
    """
    Save ALTO files for one binding on remote filesystem using SSH connection.

    :param mets_path: Path to where the METS file of the binding is stored
    """

    def __init__(self, mets_path, **kwargs):
        super().__init__(**kwargs)
        self.mets_path = mets_path

    def execute(self, context):
        path = os.path.join(
            self.mets_path, f"{utils.binding_id_from_dc(self.dc_identifier)}_METS.xml"
        )

        mets = METS(self.dc_identifier, self.sftp_client.file(path, "r"))
        alto_files = mets.files_of_type(ALTOFile)

        self.ensure_tmp_output_location()

        for alto_file in alto_files:
            temp_output_file = str(
                utils.file_download_location(
                    file=alto_file, base_path=self.tmpdir, file_dir=self.file_dir
                )
            )

            if self.file_exists(temp_output_file):
                continue

            with self.sftp_client.file(temp_output_file, "wb") as file:
                try:
                    alto_file.download(
                        output_file=file,
                        chunk_size=10 * 1024 * 1024,
                    )
                except RequestException as e:
                    self.log.error(
                        "ALTO download with URL %s failed: %s",
                        alto_file.download_url,
                        e.response,
                    )
                    continue


class DownloadBindingBatchOperator(BaseOperator):
    """
    Download a batch of bindings.

    :param batch: a list of DC identifiers
    :param ssh_conn_id: SSH connection id
    :param image_base_name: Name for disk image
    :param tmpdir: Absolute path for a temporary directory on the remote server
    :param api: OAI-PMH api
    """

    def __init__(
        self,
        batch,
        ssh_conn_id,
        image_base_name,
        tmpdir,
        api,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.batch = batch
        self.ssh_conn_id = ssh_conn_id
        self.image_base_name = image_base_name
        self.tmpdir = tmpdir
        self.api = api

    def execute(self, context):
        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()

            for dc_identifier in self.batch:
                binding_id = utils.binding_id_from_dc(dc_identifier)
                tmp_binding_path = (
                    self.tmpdir
                    / self.image_base_name
                    / utils.binding_download_location(binding_id)
                )

                SaveMetsSFTPOperator(
                    task_id=f"save_mets_{binding_id}",
                    api=self.api,
                    sftp_client=sftp_client,
                    ssh_client=ssh_client,
                    tmpdir=tmp_binding_path,
                    dc_identifier=dc_identifier,
                    file_dir="mets",
                ).execute(context={})

                SaveAltosSFTPOperator(
                    task_id=f"save_altos_{binding_id}",
                    mets_path=tmp_binding_path / "mets",
                    sftp_client=sftp_client,
                    ssh_client=ssh_client,
                    tmpdir=tmp_binding_path,
                    dc_identifier=dc_identifier,
                    file_dir="alto",
                ).execute(context={})


class PrepareDownloadLocationOperator(BaseOperator):
    """
    Prepare download location for a disk image.

    :param ssh_conn_id: SSH connection id
    :param base_path: Base path for images
    :param image_base_name: Name for disk image
    """

    def __init__(
        self,
        ssh_conn_id,
        base_path,
        tmp_path,
        image_base_name,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ssh_conn_id = ssh_conn_id
        self.base_path = base_path
        self.image_base_name = image_base_name
        self.tmp_path = tmp_path

    def extract_image(self, ssh_client, sftp_client, image_dir_path, tmp_image_path):
        """
        Extract contents of a disk image in given path to temporary storage.
        """
        ssh_client.exec_command(f"unsquashfs -d {tmp_image_path} {image_dir_path}.sqfs")

    def create_image_folder(self, sftp_client, image_dir_path):
        """
        Create folder to store image contents in.
        """
        utils.make_intermediate_dirs(
            sftp_client=sftp_client,
            remote_directory=image_dir_path,
        )

    def execute(self, context):
        tmp_image_path = self.tmp_path / self.image_base_name
        image_dir_path = self.base_path / self.image_base_name

        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            sftp_client = ssh_client.open_sftp()

            if f"{self.image_base_name}.sqfs" in sftp_client.listdir(self.base_path):
                self.extract_image(
                    ssh_client, sftp_client, image_dir_path, tmp_image_path
                )

            else:
                self.create_image_folder(sftp_client, tmp_image_path)


class CreateImageOperator(BaseOperator):
    """
    Prepare download location for a disk image.

    :param ssh_conn_id: SSH connection id
    :param base_path: Base path for images
    :param image_base_name: Name for disk image
    """

    def __init__(
        self,
        ssh_conn_id,
        tmp_path,
        base_path,
        image_base_name,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ssh_conn_id = ssh_conn_id
        self.base_path = base_path
        self.image_base_name = image_base_name
        self.tmp_path = tmp_path

    def execute(self, context):
        tmp_image_path = self.tmp_path / self.image_base_name
        final_image_location = self.base_path / self.image_base_name

        ssh_hook = SSHHook(ssh_conn_id=self.ssh_conn_id)
        with ssh_hook.get_conn() as ssh_client:
            self.log.info(f"Deleting old image {final_image_location}.sqfs")
            ssh_client.exec_command(f"rm {final_image_location}.sqfs")

            self.log.info(f"Creating image {self.image_base_name} on Puhti")
            _, stdout, stderr = ssh_client.exec_command(
                f"mksquashfs {tmp_image_path} {final_image_location}.sqfs"
            )
            if stdout.channel.recv_exit_status() != 0:
                raise Exception(
                    f"Creation of image {final_image_location}.sqfs failed: {stderr.read().decode('utf-8')}"
                )
            ssh_client.exec_command(f"rm -r {tmp_image_path}")
