# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
from abc import ABC
from typing import Any, cast, Dict, Optional

import simplejson as json
from flask import current_app as app
from flask_babel import gettext as __, lazy_gettext as _
from sqlalchemy.exc import SQLAlchemyError

from superset import db, security_manager
from superset.commands.base import BaseCommand
from superset.connectors.base.models import BaseDatasource
from superset.connectors.sqla.models import SqlaTable
from superset.dao.exceptions import DatasourceNotFound
from superset.datasource.dao import DatasourceDAO
from superset.exceptions import SupersetException
from superset.explore.commands.parameters import CommandParameters
from superset.explore.exceptions import DatasetAccessDeniedError, WrongEndpointError
from superset.explore.form_data.commands.get import GetFormDataCommand
from superset.explore.form_data.commands.parameters import (
    CommandParameters as FormDataCommandParameters,
)
from superset.explore.permalink.commands.get import GetExplorePermalinkCommand
from superset.explore.permalink.exceptions import ExplorePermalinkGetFailedError
from superset.utils import core as utils
from superset.views.utils import (
    get_datasource_info,
    get_form_data,
    sanitize_datasource_data,
)

logger = logging.getLogger(__name__)


class GetExploreCommand(BaseCommand, ABC):
    def __init__(
        self,
        params: CommandParameters,
    ) -> None:
        self._permalink_key = params.permalink_key
        self._form_data_key = params.form_data_key
        self._dataset_id = params.dataset_id
        self._dataset_type = params.dataset_type
        self._slice_id = params.slice_id

    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    def run(self) -> Optional[Dict[str, Any]]:
        initial_form_data = {}

        if self._permalink_key is not None:
            command = GetExplorePermalinkCommand(self._permalink_key)
            permalink_value = command.run()
            if not permalink_value:
                raise ExplorePermalinkGetFailedError()
            state = permalink_value["state"]
            initial_form_data = state["formData"]
            url_params = state.get("urlParams")
            if url_params:
                initial_form_data["url_params"] = dict(url_params)
        elif self._form_data_key:
            parameters = FormDataCommandParameters(key=self._form_data_key)
            value = GetFormDataCommand(parameters).run()
            initial_form_data = json.loads(value) if value else {}

        message = None

        if not initial_form_data:
            if self._slice_id:
                initial_form_data["slice_id"] = self._slice_id
                if self._form_data_key:
                    message = _(
                        "Form data not found in cache, reverting to chart metadata."
                    )
            elif self._dataset_id:
                initial_form_data[
                    "datasource"
                ] = f"{self._dataset_id}__{self._dataset_type}"
                if self._form_data_key:
                    message = _(
                        "Form data not found in cache, reverting to dataset metadata."
                    )

        form_data, slc = get_form_data(
            use_slice_data=True, initial_form_data=initial_form_data
        )
        try:
            self._dataset_id, self._dataset_type = get_datasource_info(
                self._dataset_id, self._dataset_type, form_data
            )
        except SupersetException:
            self._dataset_id = None
            # fallback unkonw datasource to table type
            self._dataset_type = SqlaTable.type

        dataset: Optional[BaseDatasource] = None
        if self._dataset_id is not None:
            try:
                dataset = DatasourceDAO.get_datasource(
                    db.session, cast(str, self._dataset_type), self._dataset_id
                )
            except DatasourceNotFound:
                pass
        dataset_name = dataset.name if dataset else _("[Missing Dataset]")

        if dataset:
            if app.config["ENABLE_ACCESS_REQUEST"] and (
                not security_manager.can_access_datasource(dataset)
            ):
                message = __(security_manager.get_datasource_access_error_msg(dataset))
                raise DatasetAccessDeniedError(
                    message=message,
                    dataset_type=self._dataset_type,
                    dataset_id=self._dataset_id,
                )

        viz_type = form_data.get("viz_type")
        if not viz_type and dataset and dataset.default_endpoint:
            raise WrongEndpointError(redirect=dataset.default_endpoint)

        form_data["datasource"] = (
            str(self._dataset_id) + "__" + cast(str, self._dataset_type)
        )

        # On explore, merge legacy and extra filters into the form data
        utils.convert_legacy_filters_into_adhoc(form_data)
        utils.merge_extra_filters(form_data)

        dummy_dataset_data: Dict[str, Any] = {
            "type": self._dataset_type,
            "name": dataset_name,
            "columns": [],
            "metrics": [],
            "database": {"id": 0, "backend": ""},
        }
        try:
            dataset_data = dataset.data if dataset else dummy_dataset_data
        except (SupersetException, SQLAlchemyError):
            dataset_data = dummy_dataset_data

        metadata = None

        if slc:
            metadata = {
                "created_on_humanized": slc.created_on_humanized,
                "changed_on_humanized": slc.changed_on_humanized,
                "owners": [owner.get_full_name() for owner in slc.owners],
                "dashboards": [
                    {"id": dashboard.id, "dashboard_title": dashboard.dashboard_title}
                    for dashboard in slc.dashboards
                ],
            }
            if slc.created_by:
                metadata["created_by"] = slc.created_by.get_full_name()
            if slc.changed_by:
                metadata["changed_by"] = slc.changed_by.get_full_name()

        return {
            "dataset": sanitize_datasource_data(dataset_data),
            "form_data": form_data,
            "slice": slc.data if slc else None,
            "message": message,
            "metadata": metadata,
        }

    def validate(self) -> None:
        pass
