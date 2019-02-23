#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) IBM Corporation 2019
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = "Tomasz Kornuta"

import abc
import logging

from ptp.utils.app_state import AppState


class Component(abc.ABC):
    def __init__(self, name, params):
        """
        Initializes the component.

        This constructor:

        - stores a pointer to ``params``:

            >>> self.params = params

        - sets a problem name:

            >>> self.name = name

        - initializes the logger.

            >>> self.logger = logging.getLogger(self.name)        

        - sets the access to ``AppState``: for dtype, visualization flag etc.

            >>> self.app_state = AppState()

        :param name: Name of the component.

        :param params: Dictionary of parameters (read from configuration ``.yaml`` file).
        """
        self.name = name
        self.params = params

        # Initialize logger.
        self.logger = logging.getLogger(self.name)        

        # Set default (empty) data definitions and default_values.
        self.data_definitions = {}
        self.default_values =  {}

        # Initialize the "name mapping facility".
        params.add_default_params({"keymappings": {}})
        self.keymappings = params["keymappings"]

        # Get access to AppState: for globals, visualization flag etc.
        self.app_state = AppState()

    @abc.abstractmethod
    def input_data_definitions(self):
        """ 
        Function returns a dictionary with definitions of input data that are required by the component.
        Abstract, must be implemented by all derived classes.

        :return: dictionary containing input data definitions (each of type :py:class:`ptp.utils.DataDefinition`).
        """
        pass

    @abc.abstractmethod
    def output_data_definitions(self):
        """ 
        Function returns a dictionary with definitions of output data produced the component.
        Abstract, must be implemented by all derived classes.

        :return: dictionary containing output data definitions (each of type :py:class:`ptp.utils.DataDefinition`).
        """
        pass

    def handshake_input_definitions(self, all_definitions, log_errors=True):
        """ 
        Checks whether all_definitions contain fields required by the given component.

        :param all_definitions: dictionary containing output data definitions (each of type :py:class:`ptp.utils.DataDefinition`).

        :param log_errors: Logs the detected errors (DEFAULT: TRUE)

        :return: number of detected errors.
        """
        errors = 0
        for (key,id) in self.input_data_definitions().items():
            # Check presence of key.
            if key not in all_definitions.keys():
                if log_errors:
                    self.logger.error("Input definition: expected field '{}' not found in DataDict keys ({})".format(key, all_definitions.keys()))
                errors += 1
                continue
            # Check number of dimensions.
            dd = all_definitions[key]
            if len(id.dimensions) != len (dd.dimensions):
                if log_errors:
                    self.logger.error("Input definition: field '{}' in DataDict has different dimensions from expected (expected {} while received {})".format(key, id.dimensions, dd.dimensions))
                errors += 1
            else: 
                # Check dimensions one by one.
                for index, (did, ddd) in enumerate(zip(id.dimensions, dd.dimensions)):
                    # -1 means that it can handle different values here.
                    if did != -1 and did != ddd:
                        if log_errors:
                            self.logger.error("Input definition: field '{}' in DataDict has dimension {} different from expected (expected {} while received {})".format(key,index, id.dimensions, dd.dimensions))
                        errors += 1
            # Check number of types.
            if len(id.types) != len (dd.types):
                if log_errors:
                    self.logger.error("Input definition: field '{}' in DataDict has number of types different from expected (expected {} while received {})".format(key, id.types, dd.types))
                errors += 1
            else: 
                # Check types one by one.
                for index, (tid, tdd) in enumerate(zip(id.types, dd.types)):
                    # -1 means that it can handle different values here.
                    if tid != tdd:
                        if log_errors:
                            self.logger.error("Input definition: field '{}' in DataDict has type {} different from expected (expected {} while received {})".format(key,index, id.types, dd.types))
                        errors += 1

        return errors
    
    def export_output_definitions(self, all_definitions, log_errors=True):
        """ 
        Exports output definitinos to all_definitions, checking errors (e.g. if output field is already present in all_definitions).

        :param all_definitions: dictionary containing output data definitions (each of type :py:class:`ptp.utils.DataDefinition`).

        :param log_errors: Logs the detected errors (DEFAULT: TRUE)

        :return: number of detected errors.
        """
        errors = 0
        for (key,od) in self.output_data_definitions().items():
            # Check presence of key.
            if key in all_definitions.keys():
                if log_errors:
                    self.logger.error("Output definition error: field '{}' cannot be added to DataDict, as it is already present in its keys ({})".format(key, all_definitions.keys()))
                errors += 1
            else:
                # Add field to definitions.
                all_definitions[key] = od

        return  errors


    def mapkey(self, key_name):
        """
        Method responsible for checking whether name exists in the mappings.
        
        :key_name: name of the key to be mapped.

        :return: Mapped name or original key name (if it does not exist in mappings list).
        """
        return self.keymappings.get(key_name, key_name)

    @abc.abstractmethod
    def __call__(self, data_dict):
        """
        Method responsible for processing the data dict.
        Abstract, must be implemented by all derived classes.

        :param data_dict: :py:class:`ptp.utils.DataDict` object containing both input data to be proces and that will be extended by the results.
        """
        pass


    def add_statistics(self, stat_col):
        """
        Adds statistics to :py:class:`ptp.utils.StatisticsCollector`.

        .. note::


            Empty - To be redefined in inheriting classes.


        :param stat_col: :py:class:`ptp.utils.StatisticsCollector`.

        """
        pass


    def collect_statistics(self, stat_col, data_dict, logits):
        """
        Base statistics collection.

         .. note::


            Empty - To be redefined in inheriting classes. The user has to ensure that the corresponding entry \
            in the :py:class:`ptp.utils.StatisticsCollector` has been created with \
            :py:func:`add_statistics` beforehand.

        :param stat_col: :py:class:`ptp.utils.StatisticsCollector`.

        :param data_dict: ``DataDict`` containing inputs and targets.
        :type data_dict: :py:class:`ptp.utils.DataDict`

        :param logits: Predictions being output of the model (:py:class:`torch.Tensor`).

        """
        pass


    def add_aggregators(self, stat_agg):
        """
        Adds statistical aggregators to :py:class:`ptp.utils.StatisticsAggregator`.

        .. note::

            Empty - To be redefined in inheriting classes.


        :param stat_agg: :py:class:`ptp.utils.StatisticsAggregator`.

        """
        pass


    def aggregate_statistics(self, stat_col, stat_agg):
        """
        Aggregates the statistics collected by :py:class:`ptp.utils.StatisticsCollector` and adds the \
        results to :py:class:`ptp.utils.StatisticsAggregator`.

         .. note::

            Empty - To be redefined in inheriting classes.
            The user can override this function in subclasses but should call \
            :py:func:`aggregate_statistics` to collect basic statistical aggregators (if set).


        :param stat_col: :py:class:`ptp.utils.StatisticsCollector`.

        :param stat_agg: :py:class:`ptp.utils.StatisticsAggregator`.

        """
        pass
