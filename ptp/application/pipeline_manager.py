# -*- coding: utf-8 -*-
#
# Copyright (C) tkornuta, IBM Corporation 2019
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


import os
import torch
from datetime import datetime
from numpy import inf,average

import ptp.components

import ptp.utils.logger as logging
from ptp.utils.app_state import AppState
from ptp.configuration.configuration_error import ConfigurationError
from ptp.application.component_factory import ComponentFactory
from ptp.utils.data_streams_parallel import DataStreamsParallel


components_to_skip_in_data_parallel = ["SentenceEmbeddings", "IndexEmbeddings"]


class PipelineManager(object):
    """
    Class responsible for instantiating the pipeline consisting of several components.
    """

    def __init__(self, name, config):
        """
        Initializes the pipeline manager.

        :param config: Parameters used to instantiate all required components.
        :type config: :py:class:`ptp.configuration.ConfigInterface`

        """
        # Initialize the logger.
        self.name = name
        self.config = config
        self.app_state = AppState()
        # Initialize logger.
        self.logger = logging.initialize_logger(self.name)        

        # Set initial values of all pipeline elements.
        # Empty list of all components, sorted by their priorities.
        self.__components = {}
        # Empty list of all models - it will contain only "references" to objects stored in the components list.
        self.models = []
        # Empty list of all losses - it will contain only "references" to objects stored in the components list.
        self.losses = []

        # Initialization of best loss - as INF.
        self.best_loss = inf
        self.best_status = "Unknown"
        # Indicates the last time when the validation loss went down.
        # 0 means currntly, 1 means during previous validation etc.
        self.validation_loss_down_counter = 0


    def build(self, use_logger=True):
        """
        Method creating the pipeline, consisting of:
            - a list components ordered by the priority (dictionary).
            - task (as a separate "link" to object in the list of components, instance of a class derrived from Task class)
            - models (separate list with link to objects in components dict)
            - losses (selarate list with links to objects in components dict)

        :param use_logger: Logs the detected errors (DEFAULT: True)

        :return: number of detected errors.
        """
        errors = 0
        self.__priorities = []

        # Special section names to "skip".
        sections_to_skip = "name load freeze disable".split()
        disabled_components = ''
        # Add components to disable by the ones from configuration file.
        if "disable" in self.config:
            disabled_components = [*disabled_components, *self.config["disable"].replace(" ","").split(",")]
        # Add components to disable by the ones from command line arguments.
        if (self.app_state.args is not None) and (self.app_state.args.disable != ''):
            disabled_components = [*disabled_components, *self.app_state.args.disable.split(",")]

        # Organize all components according to their priorities.
        for c_key, c_config in self.config.items():

            try:
                # Skip "special" pipeline sections.
                if c_key in sections_to_skip:
                    #self.logger.info("Skipping section '{}'".format(c_key))
                    continue
                # Skip "disabled" components.
                if c_key in disabled_components:
                    self.logger.info("Disabling component '{}'".format(c_key))
                    continue

                # Check presence of priority.
                if 'priority' not in c_config:
                    raise KeyError("Section '{}' does not contain the key 'priority' defining the pipeline order".format(c_key))

                # Get the priority.
                try:
                    c_priority = float(c_config["priority"])
                except ValueError:
                    raise ConfigurationError("Priority [{}] in section '{}' is not a floating point number".format(c_config["priority"], c_key))

                # Check uniqueness of the priority.
                if c_priority in self.__components.keys():
                    raise ConfigurationError("Found more than one component with the same priority [{}]".format(c_priority))

                # Ok, got the component name with priority. Save it.
                # Later we will "plug" the adequate component in this place.
                self.__components[c_priority] = c_key

            except ConfigurationError as e:
                if use_logger:
                    self.logger.error(e)
                errors += 1
                continue
            except KeyError as e:
                if use_logger:
                    self.logger.error(e)
                errors += 1
                continue
                # end try/else
            # end for

        if use_logger:
            self.logger.info("Building pipeline with {} components".format(len(self.__components)))

        # Do not continue if found errors.
        if errors > 0:
            return errors

        # Sort priorities.
        self.__priorities=sorted(self.__components.keys())        

        for c_priority in self.__priorities:
            try:
                # The section "key" will be used as "component" name.
                c_key = self.__components[c_priority]
                # Get section.
                c_config = self.config[c_key]
                
                if use_logger:
                    self.logger.info("Creating component '{}' ({}) with priority [{}]".format(c_key, c_config["type"], c_priority))

                # Create component.
                component, class_obj = ComponentFactory.build(c_key, c_config)

                # Check if class is derived (even indirectly) from Task.
                if ComponentFactory.check_inheritance(class_obj, ptp.Task.__name__):
                    raise ConfigurationError("Object '{}' cannot be instantiated as part of pipeline, \
                        as its class type '{}' is derived from Task class!".format(c_key, class_obj.__name__))

                # Add it to dict.
                self.__components[c_priority] = component

                # Check if class is derived (even indirectly) from Model.
                if ComponentFactory.check_inheritance(class_obj, ptp.Model.__name__):
                    # Add to list.
                    self.models.append(component)

                # Check if class is derived (even indirectly) from Loss.
                if ComponentFactory.check_inheritance(class_obj, ptp.Loss.__name__):
                    # Add to list.
                    self.losses.append(component)

            except ConfigurationError as e:
                if use_logger:
                    self.logger.error("Detected configuration error while creating the component '{}' instance:\n  {}".format(c_key, e))
                errors += 1
                continue
            except KeyError as e:
                if use_logger:
                    self.logger.error("Detected key error while creating the component '{}' instance: required key '{}' is missing".format(c_key, e))
                errors += 1
                continue
                # end try/else
            # end for

        # Return detected errors.
        return errors


    def save(self, chkpt_dir, training_status, loss):
        """
        Generic method saving the parameters of all models in the pipeline to a file.

        :param chkpt_dir: Directory where the model will be saved.
        :type chkpt_dir: str

        :param training_status: String representing the current status of training.
        :type training_status: str

        :return: True if this is currently the best model (until the current episode, considering the loss).
        """
        # Checkpoint to be saved.
        chkpt = {'name': self.name,
                 'timestamp': datetime.now(),
                 'episode': self.app_state.episode,
                 'loss': loss,
                 'status': training_status,
                 'status_timestamp': datetime.now(),
                }
        
        model_str = ''
        # Save state dicts of all models.
        for model in self.models:
            # Check if model is wrapped in dataparallel.
            if (type(model).__name__ == "DataStreamsParallel"):
                model.module.save_to_checkpoint(chkpt)
                model_str += "  + Model '{}' [{}] params saved \n".format(model.module.name, type(model.module).__name__)
            else:
                model.save_to_checkpoint(chkpt)
                model_str += "  + Model '{}' [{}] params saved \n".format(model.name, type(model).__name__)

        # Save the intermediate checkpoint.
        if self.app_state.args.save_intermediate:
            filename = chkpt_dir + self.name + '_episode_{:05d}.pt'.format(self.app_state.episode)
            torch.save(chkpt, filename)
            log_str = "Exporting pipeline '{}' parameters to checkpoint:\n {}\n".format(self.name, filename)
            log_str += model_str
            self.logger.info(log_str)

        # Save the best "model".
        # loss = loss.cpu()  # moving loss value to cpu type to allow (initial) comparison with numpy type
        if loss < self.best_loss:
            # Save best loss and status.
            self.best_loss = loss
            self.best_status = training_status
            # Save checkpoint.
            filename = chkpt_dir + self.name + '_best.pt'
            torch.save(chkpt, filename)
            log_str = "Exporting pipeline '{}' parameters to checkpoint:\n {}\n".format(self.name, filename)
            log_str += model_str
            self.logger.info(log_str)
            # Ok, loss went down, reset the counter.
            self.validation_loss_down_counter = 0
            return True
        elif self.best_status != training_status:
            filename = chkpt_dir + self.name + '_best.pt'
            # Load checkpoint.
            chkpt_loaded = torch.load(filename, map_location=lambda storage, loc: storage)
            # Update status and status time.
            chkpt_loaded['status'] = training_status
            chkpt_loaded['status_timestamp'] = datetime.now()
            # Save updated checkpoint.
            torch.save(chkpt_loaded, filename)
            self.logger.info("Updated training status in checkpoint:\n {}".format(filename))
        # Else: that was not the best "model".
        # Loss didn't went down, increment the counter.
        self.validation_loss_down_counter += 1
        return False

    def load(self, checkpoint_file):
        """
        Loads parameters of models in the pipeline from the specified checkpoint file.

        :param checkpoint_file: File containing dictionary with states of all models in the pipeline with some additional checkpoint statistics.

        """
        # Load checkpoint
        checkpoint_file = os.path.expanduser(checkpoint_file.replace(" ",""))
        # This is to be able to load a CUDA-trained model on CPU
        chkpt = torch.load(checkpoint_file, map_location=lambda storage, loc: storage)

        log_str = "Loading models constituting the '{}' pipeline from checkpoint defined in {} (episode: {}, loss: {}, status: {}):\n".format(
                chkpt['name'],
                chkpt['timestamp'],
                chkpt['episode'],
                chkpt['loss'],
                chkpt['status']
                )
        model_str = ''
        warning = False
        # Save state dicts of all models.
        for model in self.models:
            try:
                # Load model.
                model.load_from_checkpoint(chkpt)
                model_str += "  + Model '{}' [{}] params loaded\n".format(model.name, type(model).__name__)
            except KeyError:
                model_str += "  + Model '{}' [{}] params not found in checkpoint!\n".format(model.name, type(model).__name__)
                warning = True

        # Log results.
        log_str += model_str
        if warning:
            self.logger.warning(log_str)
        else:
            self.logger.info(log_str)

    def load_models(self):
        """
        Method analyses the configuration and loads models one by one by looking whether they got 'load' variable present in their configuration section.

        ..note::
            The 'load' variable should contain path with filename of the checkpoint from which we want to load particular model.
        """
        error = False
        log_str = ''
        # Iterate over models.
        for model in self.models:
            if "load" in model.config.keys():
                try:
                    # Determine whether checkpoint is a string (filename) or list.
                    checkpoint = model.config["load"]
                    if type(checkpoint) == str:
                        checkpoint_filename = checkpoint
                        checkpoint_model = None
                    else: # Assume dictionary.
                        if 'file' not in checkpoint.keys() or 'model' not in checkpoint.keys():
                            log_str += "  + The 'load' section of model '{}' is incorrect: it must contain a single string (with checkpoint filename) or a dictionary (with two sections: checkpoint 'file' and 'model' to load)\n".format(
                                model.name
                                )
                            error = True
                            continue
                        # Ok!
                        checkpoint_filename = checkpoint["file"]
                        checkpoint_model = checkpoint["model"]

                    # Check if file exists. 
                    checkpoint_filename = os.path.expanduser(checkpoint_filename.replace(" ",""))
                    if not os.path.isfile(checkpoint_filename):
                        log_str += "  + Could not import parameters of model '{}' from checkpoint '{}' as file does not exist\n".format(
                            model.name,
                            checkpoint_filename
                            )
                        error = True
                        continue

                    # Load checkpoint.
                    # This is to be able to load a CUDA-trained model on CPU
                    chkpt = torch.load(checkpoint_filename, map_location=lambda storage, loc: storage)

                    log_str += "  + Importing model '{}' from pipeline '{}' parameters from checkpoint from {} (episode: {}, loss: {}, status: {})\n".format(
                            model.name,
                            chkpt['name'],
                            chkpt['timestamp'],
                            chkpt['episode'],
                            chkpt['loss'],
                            chkpt['status']
                            )
                    # Load model.
                    model.load_from_checkpoint(chkpt, checkpoint_model)

                    log_str += "  + Model '{}' [{}] params loaded\n".format(model.name, type(model).__name__)
                except KeyError:
                    log_str += "  + Model '{}' [{}] params not found in checkpoint!\n".format(model.name, type(model).__name__)
                    error = True

        # Log results.
        if error:
            # Log errors - always.
            log_str = 'Failed while trying to load the pre-trained models:\n' + log_str
            self.logger.error(log_str)
            # Exit by following the logic: if user wanted to load the model but failed, then continuing the experiment makes no sense.
            exit(-6)
        else:
            # Log info - only if some models were loaded.
            if len(log_str) > 0:
                log_str = 'Successfully loaded the pre-trained models:\n' + log_str
                self.logger.info(log_str)


    def freeze_models(self):
        """
        Method analyses the configuration and freezes:
            - all models when 'freeze' flag for whoe pipeline is set,
            - individual models when their 'freeze' flags are set.
        """
        # Check freeze all option.
        if "freeze" in self.config.keys():
            freeze_all = bool(self.config["freeze"])
        else: 
            freeze_all = False
                
        # Iterate over models.
        for model in self.models:
            if "freeze" in model.config.keys():
                if bool(model.config["freeze"]):
                    model.freeze()
            elif freeze_all:
                model.freeze()
        

    def __getitem__(self, number):
        """
        Returns the component, using the enumeration resulting from priorities.

        :param number: Number of the component in the pipeline.
        :type key: str

        :return: object of type :py:class:`Component`.

        """
        return self.__components[self.__priorities[number]]


    def __len__(self):
        """
        Returns the number of objects in the pipeline (excluding tasks)
        :return: Length of the :py:class:`Pipeline`.

        """
        length = len(self.__priorities) 
        return length


    def summarize_all_components_header(self):
        """
        Creates the summary header containing components with inputs-outputs definitions.

        :return: Summary header as a str.
        """
        summary_str  = 'Summary of the created pipeline:\n'
        summary_str += '='*80 + '\n'
        summary_str += 'Pipeline\n'
        summary_str += '  + Component name (type) [priority]\n'
        summary_str += '      Inputs:\n' 
        summary_str += '        key: dims, types, description\n'
        summary_str += '      Outputs:\n' 
        summary_str += '        key: dims, types, description\n'
        summary_str += '=' * 80 + '\n'
        return summary_str


    def summarize_all_components(self):
        """
        Summarizes the pipeline by showing all its components (excluding task).

        :return: Summary as a str.
        """
        summary_str = '' 
        for prio in self.__priorities:
            # Get component
            comp = self.__components[prio]
            if type(comp) == str:
                summary_str += '  + {} (None: not created) [{}]\n'.format(comp, prio)
            else:
                summary_str += comp.summarize_io(prio)
        summary_str += '=' * 80 + '\n'
        return summary_str

    def summarize_models_header(self):
        """
        Creates the summary header containing details of models.

        :return: Summary header as a str.
        """
        summary_str  = 'Summary of the models in the pipeline:\n'
        summary_str += '='*80 + '\n'
        summary_str += 'Model name (Type) \n'
        summary_str += '  + Submodule name (Type) \n'
        summary_str += '      Matrices: [(name, dims), ...]\n'
        summary_str += '      Trainable Params: #\n'
        summary_str += '      Non-trainable Params: #\n'
        summary_str += '=' * 80 + '\n'
        return summary_str

    def summarize_models(self):
        """
        Summarizes the pipeline by showing all its components (excluding task).

        :return: Summary as a str.
        """
        summary_str = '' 
        for model in self.models:
            summary_str += model.summarize()
        return summary_str


    def handshake(self, data_streams, log=True):
        """
        Performs handshaking of inputs and outputs definitions of all components in the pipeline.

        :param data_streams: Initial datadict returned by the task.

        :param log: Logs the detected errors and info (DEFAULT: True)

        :return: Number of detected errors.
        """
        errors = 0

        for prio in self.__priorities:
            # Get component
            comp = self.__components[prio]
            # Handshake inputs and outputs.
            errors += comp.handshake_input_definitions(data_streams, log)
            errors += comp.export_output_definitions(data_streams, log)

        # Log final definition.
        if errors == 0 and log:
            self.logger.info("Handshake successfull")
            def_str = "Final definition of DataStreams used in pipeline:\n"
            def_str += '='*80 + '\n'
            for item in data_streams.items():
                def_str += '  {}\n'.format(item)
            def_str += '='*80 + '\n'
            self.logger.info(def_str)

        return errors


    def forward(self, data_streams):
        """
        Method responsible for processing the data dict, using all components in the components queue.

        :param data_streams: :py:class:`ptp.utils.DataStreams` object containing both input data to be processed and that will be extended by the results.

        """
        if self.app_state.args.use_gpu:
            data_streams.to(device = self.app_state.device)

        for prio in self.__priorities:
            # Get component
            comp = self.__components[prio]
            if (type(comp).__name__ == "DataStreamsParallel"):
                # Forward of wrapper returns outputs in separate DataStreams.
                outputs = comp(data_streams)
                # Postprocessing: copy only the outputs of the wrapped model.
                for key in comp.module.output_data_definitions().keys():
                    data_streams.publish({key: outputs[key]})
            else: 
                # "Normal" forward step.
                comp(data_streams)
                # Move data to device.
                data_streams.to(device = self.app_state.device)


    def eval(self):
        """ 
        Sets evaluation mode for all models in the pipeline.
        """
        for model in self.models:
            model.eval


    def train(self):
        """ 
        Sets evaluation mode for all models in the pipeline.
        """
        for model in self.models:
            model.train()


    def cuda(self):
        """ 
        Moves all models to GPU.
        """
        self.logger.info("Moving model(s) to GPU(s)")
        if self.app_state.use_dataparallel:
            self.logger.info("Using data parallelization on {} GPUs!".format(torch.cuda.device_count()))

        # Regenerate the model list AND overwrite the models on the list of components.
        self.models = []
        for key, component in self.__components.items():

            # Check if class is derived (even indirectly) from Model.
            if ComponentFactory.check_inheritance(type(component), ptp.Model.__name__):
                model = component
                # Wrap model with DataStreamsParallel when required.
                if self.app_state.use_dataparallel and type(model).__name__ not in components_to_skip_in_data_parallel:
                    print("Moving to GPU", model.name)
                    model = DataStreamsParallel(model)
                # Mode to cuda.
                model.to(self.app_state.device)

                # Add to list.
                self.models.append(model)
                # "Overwrite" model on the component list.
                self.__components[key] = model

    def zero_grad(self):
        """ 
        Resets gradients in all trainable components of the pipeline.
        """
        for model in self.models:
            model.zero_grad()


    def backward(self, data_streams):
        """
        Propagates gradients backwards, starting from losses returned by every loss component in the pipeline.
        If using many losses the components derived from loss must overwrite the ''loss_keys()'' method.

        :param data_streams: :py:class:`ptp.utils.DataStreams` object containing both input data to be processed and that will be extended by the results.

        """
        if (len(self.losses) == 0):
            raise ConfigurationError("Cannot train using backpropagation as there are no 'Loss' components")
        # Calculate total number of backward passes.
        total_passes = sum([len(loss.loss_keys()) for loss in self.losses])

        # All but the last call to backward should have the retain_graph=True option.
        pass_counter = 0
        for loss in self.losses:
            for key in loss.loss_keys():
                pass_counter += 1
                if pass_counter == total_passes:
                    # Last pass.
                    data_streams[key].backward()
                else:
                    # "Other pass."
                    data_streams[key].backward(retain_graph=True)


    def return_loss_on_batch(self, stat_col):
        """
        Sums all losses and returns a single value that can be used e.g. in terminal condition or model(s) saving.

        :param data_streams: :py:class:`ptp.utils.DataStreams` object containing both input data to be processed and that will be extended by the results.

        :return: Loss (scalar value).
        """
        return stat_col["total_loss"][-1]


    def return_loss_on_set(self, stat_agg):
        """
        Sums all losses and returns a single value that can be used e.g. in terminal condition or model(s) saving.

        :param data_streams: :py:class:`ptp.utils.DataStreams` object containing both input data to be processed and that will be extended by the results.

        :return: Loss (scalar value).
        """

        return stat_agg["total_loss"]


    def parameters(self, recurse=True):
        """
        Returns an iterator over parameters of all trainable components.

        This is typically passed to an optimizer.

        Args:
            recurse (bool): if True, then yields parameters of this module
                and all submodules. Otherwise, yields only parameters that
                are direct members of this module.

        Yields:
            Parameter: module parameter

        Example::

        """
        for model in self.models:
            for _, param in model.named_parameters(recurse=recurse):
                yield param


    def named_parameters(self, recurse=True):
        """
        Returns an iterator over all named parameters of all trainable components.
        """
        for model in self.models:
            for name, param in model.named_parameters(recurse=recurse):
                yield name, param


    def add_statistics(self, stat_col):
        """
        Adds statistics for every component in the pipeline.

        :param stat_col: ``StatisticsCollector``.

        """
        for prio in self.__priorities:
            comp = self.__components[prio]
            comp.add_statistics(stat_col)

        # Check number of losses in the pipeline.
        num_losses = 0
        for loss in self.losses:
            num_losses += len(loss.loss_keys())
        self.show_total_loss = (num_losses > 1)

        # Additional "total loss" (for single- and multi-loss pipelines).
        # Collect it always, but show it only for multi-loss pipelines.
        if self.show_total_loss:
            stat_col.add_statistics("total_loss", '{:12.10f}')
        else:
            stat_col.add_statistics("total_loss", None)
        stat_col.add_statistics("total_loss_support", None)


    def collect_statistics(self, stat_col, data_streams):
        """
        Collects statistics for every component in the pipeline.

        :param stat_col: :py:class:`ptp.utils.StatisticsCollector`.

        :param data_streams: ``DataStreams`` containing inputs, targets etc.
        :type data_streams: :py:class:`ptp.data_types.DataStreams`

        """
        for prio in self.__priorities:
            comp = self.__components[prio]
            comp.collect_statistics(stat_col, data_streams)

        # Additional "total loss" (for single- and multi-loss pipelines).
        loss_sum = 0
        for loss in self.losses:
            for key in loss.loss_keys():
                loss_sum += data_streams[key].cpu().item()
        stat_col["total_loss"] = loss_sum
        stat_col["total_loss_support"] = len(data_streams["indices"]) # batch size


    def add_aggregators(self, stat_agg):
        """
        Aggregates statistics by calling adequate aggregation method of every component in the pipeline.

        :param stat_agg: ``StatisticsAggregator``.

        """
        for prio in self.__priorities:
            comp = self.__components[prio]
            comp.add_aggregators(stat_agg)

        # Additional "total loss" (for single- and multi-loss pipelines).
        # Collect it always, but show it only for multi-loss pipelines.
        if self.show_total_loss:
            stat_agg.add_aggregator("total_loss", '{:12.10f}')  
        else:
            stat_agg.add_aggregator("total_loss", None)  


    def aggregate_statistics(self, stat_col, stat_agg):
        """
        Aggregates statistics by calling adequate aggregation method of every component in the pipeline.

        :param stat_col: ``StatisticsCollector``

        :param stat_agg: ``StatisticsAggregator``

        """
        for prio in self.__priorities:
            comp = self.__components[prio]
            comp.aggregate_statistics(stat_col, stat_agg)

        # Additional "total loss" (for single- and multi-loss pipelines).
        total_losses = stat_col["total_loss"]
        supports = stat_col["total_loss_support"]

        # Special case - no samples!
        if sum(supports) == 0:
            stat_agg.aggregators["total_loss"] = 0
        else: 
            # Calculate default aggregate - weighted mean.
            stat_agg.aggregators["total_loss"] = average(total_losses, weights=supports)
