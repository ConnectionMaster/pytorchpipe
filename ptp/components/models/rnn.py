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

import torch
import torch.nn.functional as F

from ptp.configuration.configuration_error import ConfigurationError
from ptp.components.models.model import Model
from ptp.data_types.data_definition import DataDefinition


class RNN(Model): 
    """
    Simple Classifier consisting of fully connected layer with log softmax non-linearity.
    """
    def __init__(self, name, params):
        """
        Initializes the model.

        :param params: Dictionary of parameters (read from configuration ``.yaml`` file).
        """
        # Call constructors of parent classes.
        #super(Model, self).__init__(name, params))
        Model.__init__(self, name, RNN, params)

        # Set key mappings.
        self.key_inputs = self.get_stream_key("inputs")
        self.key_predictions = self.get_stream_key("predictions")

        # Retrieve input size from global params.
        self.key_input_size = self.get_global_key("input_size")
        self.input_size = self.app_state[self.key_input_size]
        if type(self.input_size) == list:
            if len(self.input_size) == 1:
                self.input_size = self.input_size[0]
            else:
                raise ConfigurationError("RNN input size '{}' must be a single dimension (current {})".format(self.key_input_size, self.input_size))

        # Retrieve output (prediction) size from global params.
        self.key_prediction_size = self.get_global_key("prediction_size")
        self.prediction_size = self.app_state[self.key_prediction_size]
        if type(self.prediction_size) == list:
            if len(self.prediction_size) == 1:
                self.prediction_size = self.prediction_size[0]
            else:
                raise ConfigurationError("RNN prediction size '{}' must be a single dimension (current {})".format(self.key_prediction_size, self.prediction_size))
        
        # Retrieve hidden size from configuration.
        self.hidden_size = self.params["hidden_size"]
        if type(self.hidden_size) == list:
            if len(self.hidden_size) == 1:
                self.hidden_size = self.hidden_size[0]
            else:
                raise ConfigurationError("RNN hidden_size must be a single dimension (current {})".format(self.hidden_size))
        
        self.logger.info("Initializing RNN with input size = {}, hidden size = {} and prediction size = {}".format(self.input_size, self.hidden_size, self.prediction_size))

        # Get dropout value from config.
        dropout_rate = self.params["dropout_rate"]
        # Create dropout layer.
        self.dropout = torch.nn.Dropout(dropout_rate)

        # Get number of layers from config.
        self.num_layers = self.params["num_layers"]

        # Create RNN depending on the configuration
        self.rnn_type = self.params["rnn_type"]
        if self.rnn_type in ['LSTM', 'GRU']:
            # Create rnn cell.
            self.rnn = getattr(torch.nn, self.rnn_type)(self.input_size, self.hidden_size, self.num_layers, dropout=dropout_rate)
        else:
            try:
                # Retrieve the non-linearity.
                nonlinearity = {'RNN_TANH': 'tanh', 'RNN_RELU': 'relu'}[self.rnn_type]
                # Create rnn cell.
                self.rnn = torch.nn.RNN(self.input_size, self.hidden_size, self.num_layers, nonlinearity=nonlinearity, dropout=dropout_rate)

            except KeyError:
                raise ConfigurationError( "Invalid RNN type, available options for 'rnn_type' are ['LSTM', 'GRU', 'RNN_TANH', 'RNN_RELU'] (currently '{}')".format(self.rnn_type))
        
        # Create the output layer.
        self.hidden2output = torch.nn.Linear(self.hidden_size, self.prediction_size)
        

    def init_hiddens_state(self, batch_size):

        if self.rnn_type == 'LSTM':
            # Return tuple (hidden_state, memory_cell).
            return (torch.zeros(self.num_layers, batch_size, self.hidden_size),
                    torch.zeros(self.num_layers, batch_size, self.hidden_size) )
        else:
            # Return hidden_state.
            return torch.zeros(self.num_layers, batch_size, self.hidden_size)


    def input_data_definitions(self):
        """ 
        Function returns a dictionary with definitions of input data that are required by the component.

        :return: dictionary containing input data definitions (each of type :py:class:`ptp.utils.DataDefinition`).
        """
        return {
            self.key_inputs: DataDefinition([-1, -1, self.input_size], [torch.Tensor], "Batch of inputs, each represented as index [BATCH_SIZE x SEQ_LEN x INPUT_SIZE]"),
            }


    def output_data_definitions(self):
        """ 
        Function returns a dictionary with definitions of output data produced the component.

        :return: dictionary containing output data definitions (each of type :py:class:`ptp.utils.DataDefinition`).
        """
        return {
            self.key_predictions: DataDefinition([-1, -1, self.prediction_size], [torch.Tensor], "Batch of predictions, each represented as probability distribution over classes [BATCH_SIZE x SEQ_LEN x PREDICTION_SIZE]")
            }


    def forward(self, data_dict):
        """
        Forward pass of the model.

        :param data_dict: DataDict({'inputs', 'predictions ...}), where:

            - inputs: expected inputs [BATCH_SIZE x SEQ_LEN x INPUT_SIZE],
            - predictions: returned output with predictions (log_probs) [BATCH_SIZE x SEQ_LEN x PREDICTION_SIZE]
        """

        # Get inputs [BATCH_SIZE x SEQ_LEN x INPUT_SIZE]
        inputs = data_dict[self.key_inputs]

        # Initialize hidden state.
        hidden = self.init_hiddens_state(inputs.shape[0])

        # Propagate inputs through rnn.
        activations, hidden = self.rnn(inputs, hidden)
        
        # Propagate activations through dropout layer.
        activations = self.drop(activations)

        # Reshape to 2D tensor [BATCH_SIZE * SEQ_LEN x HIDDEN_SIZE]
        outputs = activations.view(activations.size(0)*activations.size(1), activations.size(2))

        # Propagate data through the output layer [BATCH_SIZE * SEQ_LEN x PREDICTION_SIZE]
        outputs = self.hidden2output(outputs)

        # Reshape back to 3D tensor [BATCH_SIZE x SEQ_LEN x PREDICTION_SIZE]
        outputs = outputs.view(activations.size(0), activations.size(1), outputs.size(1))

        # Log softmax - along PREDICTION dim.
        predictions = F.log_softmax(outputs, dim=2)

        # Add predictions to datadict.
        data_dict.extend({self.key_predictions: predictions})