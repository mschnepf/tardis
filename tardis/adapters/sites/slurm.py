from ...configuration.configuration import Configuration
from ...exceptions.executorexceptions import CommandExecutionFailure
from ...exceptions.tardisexceptions import TardisError
from ...exceptions.tardisexceptions import TardisTimeout
from ...exceptions.tardisexceptions import TardisResourceStatusUpdateFailed
from ...interfaces.siteadapter import ResourceStatus
from ...interfaces.siteadapter import SiteAdapter
from ...utilities.staticmapping import StaticMapping
from ...utilities.attributedict import AttributeDict
from ...utilities.attributedict import convert_to_attribute_dict
from ...utilities.executors.shellexecutor import ShellExecutor
from ...utilities.asynccachemap import AsyncCacheMap
from ...utilities.utils import htcondor_csv_parser

from asyncio import TimeoutError
from contextlib import contextmanager
from functools import partial
from datetime import datetime

import logging
import re


async def slurm_status_updater(executor):
    attributes = dict(JobId="%A", Host="%N", State="%T")
    attributes_string = "|".join(attributes.values())
    cmd = f'squeue -o "{attributes_string}" -h -t all'

    slurm_resource_status = {}
    logging.debug("Slurm status update is started.")
    try:
        slurm_status = await executor.run_command(cmd)
    except CommandExecutionFailure as cf:
        logging.error(f"Slurm status update has failed due to {cf}.")
        raise
    else:
        for row in htcondor_csv_parser(
            slurm_status.stdout, fieldnames=tuple(attributes.keys()), delimiter="|"
        ):
            row["State"] = row["State"].strip()
            slurm_resource_status[row["JobId"]] = row
        logging.debug("Slurm status update finished.")
        return slurm_resource_status


class SlurmAdapter(SiteAdapter):
    def __init__(self, machine_type: str, site_name: str):
        self._configuration = getattr(Configuration(), site_name)
        self._machine_type = machine_type
        self._site_name = site_name
        self._startup_command = self._configuration.StartupCommand

        self._executor = getattr(self._configuration, "executor", ShellExecutor())

        self._slurm_status = AsyncCacheMap(
            update_coroutine=partial(slurm_status_updater, self._executor),
            max_age=self._configuration.StatusUpdate * 60,
        )

        key_translator = StaticMapping(
            remote_resource_uuid="JobId", resource_status="State"
        )

        # see job state codes at https://slurm.schedmd.com/squeue.html#lbAG
        translator_functions = StaticMapping(
            State=lambda x, translator=StaticMapping(
                CANCELLED=ResourceStatus.Deleted,
                COMPLETED=ResourceStatus.Deleted,
                COMPLETING=ResourceStatus.Running,
                CONFIGURING=ResourceStatus.Booting,
                PENDING=ResourceStatus.Booting,
                PREEMPTED=ResourceStatus.Deleted,
                RESV_DEL_HOLD=ResourceStatus.Stopped,
                REQUEUE_FED=ResourceStatus.Booting,
                REQUEUE_HOLD=ResourceStatus.Booting,
                REQUEUED=ResourceStatus.Booting,
                RESIZING=ResourceStatus.Running,
                RUNNING=ResourceStatus.Running,
                SIGNALING=ResourceStatus.Running,
                SPECIAL_EXIT=ResourceStatus.Booting,
                STAGE_OUT=ResourceStatus.Running,
                STOPPED=ResourceStatus.Stopped,
                SUSPENDED=ResourceStatus.Stopped,
            ): translator.get(x, default=ResourceStatus.Error),
            JobId=lambda x: int(x),
        )

        self.handle_response = partial(
            self.handle_response,
            key_translator=key_translator,
            translator_functions=translator_functions,
        )

    async def deploy_resource(
        self, resource_attributes: AttributeDict
    ) -> AttributeDict:
        request_command = (
            f"sbatch -p {self.machine_type_configuration.Partition} "
            f"-N 1 -n {self.machine_meta_data.Cores} "
            f"--mem={self.machine_meta_data.Memory}gb "
            f"-t {self.machine_type_configuration.Walltime} "
            f"--export=SLURM_Walltime="
            f"{self.machine_type_configuration.Walltime} "
            f"{self._startup_command}"
        )
        result = await self._executor.run_command(request_command)
        logging.debug(f"{self.site_name} sbatch returned {result}")
        pattern = re.compile(r"^Submitted batch job (\d*)", flags=re.MULTILINE)
        remote_resource_uuid = int(pattern.findall(result.stdout)[0])
        resource_attributes.update(
            remote_resource_uuid=remote_resource_uuid,
            created=datetime.now(),
            updated=datetime.now(),
            drone_uuid=self.drone_uuid(str(remote_resource_uuid)),
            resource_status=ResourceStatus.Booting,
        )
        return resource_attributes

    async def resource_status(
        self, resource_attributes: AttributeDict
    ) -> AttributeDict:
        await self._slurm_status.update_status()
        try:
            resource_uuid = resource_attributes.remote_resource_uuid
            resource_status = self._slurm_status[str(resource_uuid)]
        except KeyError:
            if (
                self._slurm_status.last_update - resource_attributes.created
            ).total_seconds() < 0:
                # In case the created timestamp is after last update timestamp of the
                # asynccachemap, no decision about the current state can be given,
                # since map is updated asynchronously. Just retry later on.
                raise TardisResourceStatusUpdateFailed
            else:
                resource_status = {
                    "JobID": resource_attributes.remote_resource_uuid,
                    "State": "COMPLETED",
                }
        logging.debug(f"{self.site_name} has status {resource_status}.")
        resource_attributes.update(updated=datetime.now())
        return convert_to_attribute_dict(
            {**resource_attributes, **self.handle_response(resource_status)}
        )

    async def terminate_resource(self, resource_attributes: AttributeDict):
        request_command = f"scancel {resource_attributes.remote_resource_uuid}"
        await self._executor.run_command(request_command)
        resource_attributes.update(
            resource_status=ResourceStatus.Stopped, updated=datetime.now()
        )
        return self.handle_response(
            {"JobId": resource_attributes.remote_resource_uuid}, **resource_attributes
        )

    async def stop_resource(self, resource_attributes: AttributeDict):
        logging.debug("Slurm jobs cannot be stopped gracefully. Terminating instead.")
        return await self.terminate_resource(resource_attributes)

    @contextmanager
    def handle_exceptions(self):
        try:
            yield
        except CommandExecutionFailure as ex:
            logging.info("Execute command failed: %s" % str(ex))
            raise TardisResourceStatusUpdateFailed
        except TardisResourceStatusUpdateFailed:
            raise
        except TimeoutError as te:
            raise TardisTimeout from te
        except Exception as ex:
            raise TardisError from ex
