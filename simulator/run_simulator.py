# Simulate inference of a Transformer model in the FlexiBERT 2.0 framework on AccelTran

import os
import sys
import json
import yaml
import time
import shutil
import argparse
from tqdm import tqdm
from matplotlib import pyplot as plt

sys.path.append('./src/')

from pe import *
from ops import *
from utils import *
from buffer import *
from tiled_ops import *
from accelerator import *
from dict2ops import main as dict2ops


SEQ_LENGTH = 512
DEBUG = False
OVERWRITE_PLOT_STEPS_WITH_DEBUG = False
OVERWRITE_LOGS = True


def check_config(config: dict, design_space: dict):
	"""Check if the accelerator configuration comes under the allowed design space"""
	assert config['tile']['tile_b'] in design_space['tile']['tile_b'], f'Configuration "tile_b" ({config["tile"]["tile_b"]}) not in {design_space["tile"]["tile_b"]}'
	assert config['tile']['tile_x'] == config['tile']['tile_y'], f'Configuration "tile_x" ({config["tile"]["tile_x"]}) and "tile_y" ({config["tile"]["tile_y"]}) should match'
	assert config['tile']['tile_x'] in design_space['tile']['tile_x'], f'Configuration "tile_x" ({config["tile"]["tile_b"]}) not in {design_space["tile"]["tile_x"]}'
	assert config['tile']['tile_y'] in design_space['tile']['tile_y'], f'Configuration "tile_y" ({config["tile"]["tile_y"]}) not in {design_space["tile"]["tile_y"]}'

	assert config['non_linearity'] in design_space['non_linearity'], f'Configuration "non_linearity" ({config["non_linearity"]}) not in {design_space["non_linearity"]}'
	assert config['pe'] in design_space['pe'], f'Configuration "pe" ({config["pe"]}) should be in {design_space["pe"]}'
	assert config['lanes_per_pe'] in design_space['lanes_per_pe'], f'Configuration "lanes_per_pe" ({config["lanes_per_pe"]}) not in {design_space["lanes_per_pe"]}'
	assert config['mac_per_lane'] in design_space['mac_per_lane'], f'Cofiguration "mac_per_lane" ({config["mac_per_lane"]}) not in {design_space["mac_per_lane"]}'

	assert config['tile']['tile_b'] <= config['batch_size'], f'Configuraton "tile_b" ({config["tile"]["tile_b"]}) should be less than or equal to "batch_size" ({config["batch_size"]})'
	assert config['batch_size'] in design_space['batch_size'], f'Configuration "batch_size" not in {design_space["batch_size"]}'

	assert config['activation_buffer_size'] in design_space['activation_buffer_size'], f'Configuration "activation_buffer_size" not in {design_space["activation_buffer_size"]}'
	assert config['weight_buffer_size'] in design_space['weight_buffer_size'], f'Configuration "weight_buffer_size" not in {design_space["weight_buffer_size"]}'
	assert config['mask_buffer_size'] in design_space['mask_buffer_size'], f'Configuration "mask_buffer_size" not in {design_space["mask_buffer_size"]}'

	assert config['main_memory']['type'] in design_space['main_memory_config'].keys(), f'Unsupported main memory type ({config["main_memory"]["type"]})'
	memory_config = {'banks': config['main_memory']['banks'], 'ranks': config['main_memory']['ranks'], 'channels': config['main_memory']['channels']}
	assert memory_config in design_space['main_memory_config'][config['main_memory']['type']], f'Unsupported main memory configuration ({memory_config})'


def get_op_list(ops, op_idx):
	assert type(op_idx) == tuple

	if op_idx is not None:
		if type(ops[op_idx[0]]) == list:
			assert len(op_idx[1]) != 0
			op = []
			for head_idx, head_op in enumerate(ops[op_idx[0]]):
				op.append(head_op[op_idx[1][head_idx]])
			return op
		else:
			return [ops[op_idx[0]]]
	else:
		return []


def get_last_compute_op(head_op, head_idx, memory_op_idx, compute_ops):
	"""Get the last compute_op corresponding to the current storing memory_op"""

	last_compute_op = None
	compute_op_found = False

	if memory_op_idx[1] != []:
		head_ops = compute_ops[memory_op_idx[0]][head_idx]
	else:
		head_ops = compute_ops

	for i, compute_op in enumerate(head_ops):
		if type(compute_op) == list: continue
		if compute_op.op_name.startswith(head_op.op_name[:-2]):
			last_compute_op_idx = i
			compute_op_found = True
		elif compute_op_found:
			return head_ops[last_compute_op_idx]


def prev_memory_op_done(head_op, head_idx, memory_op_idx, memory_ops):
	"""Mark previous memory_op as done"""

	if memory_op_idx[1] != []:
		curr_idx = memory_op_idx[1][head_idx]
		head_ops = memory_ops[memory_op_idx[0]][head_idx]
	else:
		curr_idx = memory_op_idx[0]
		head_ops = memory_ops

	for prev_op_idx in range(curr_idx - 1, -1, -1):
		if type(head_ops[prev_op_idx]) == list: continue
		if head_ops[prev_op_idx].data_type == head_op.data_type:
			head_ops[prev_op_idx].done = True
			break


def update_op_idx(ops, op_idx, stall_list, ops_done):
	# Update op_idx based on stalls
	for head_idx, stall in enumerate(stall_list):
		if stall:
			# corresponding memory_op remains the same
			pass
		else:
			ops_done += 1
			if len(stall_list) > 1:
				if op_idx[1][head_idx] < len(ops[op_idx[0]][head_idx]) - 1:
					op_idx[1][head_idx] += 1
				else:
					op_idx[1][head_idx] = None
			else:
				op_idx[0] += 1
				if type(ops[op_idx[0]]) == list:
					op_idx[1] = [0] * len(ops[op_idx[0]])
				else:
					op_idx[1] = []

	# If all ops are done in a multi-head op, proceed to next op
	if len(stall_list) > 1 and all([op_idx[1][head_idx] is None for head_idx in range(len(stall_list))]):
		op_idx[0] += 1
		if type(ops[op_idx[0]]) == list:
			op_idx[1] = [0] * len(ops[op_idx[0]])
		else:
			op_idx[1] = []

	if op_idx[0] > len(ops) - 1:
		op_idx = None

	return op_idx, ops_done


def get_utilization(accelerator):
	mac_lanes_used = 0
	for pe in accelerator.pes:
		for mac_lane in pe.mac_lanes:
			mac_lanes_used += 0 if mac_lane.ready else 1

	return mac_lanes_used * 1.0 / len(accelerator.pes) / len(accelerator.pes[0].mac_lanes), accelerator.activation_buffer.used * 1.0 / accelerator.activation_buffer.buffer_size, accelerator.weight_buffer.used * 1.0 / accelerator.weight_buffer.buffer_size, accelerator.mask_buffer.used * 1.0 / accelerator.mask_buffer.buffer_size


def log_energy(logs, total_pe_energy, activation_buffer_energy, weight_buffer_energy, mask_buffer_energy, logs_dir, accelerator, plot_steps):
	"""Log energy values for every cycle"""
	if logs:
		last_cycle = logs['cycle'][-1]
		cycle_difference = accelerator.cycle - last_cycle
		assert cycle_difference > 0

		for c in range(last_cycle + 1, accelerator.cycle + 1):
			logs['cycle'].append(c)
			logs['total_pe_energy'].append((total_pe_energy[0] / cycle_difference, total_pe_energy[1] / cycle_difference))
			logs['activation_buffer_energy'].append((activation_buffer_energy[0] / cycle_difference, activation_buffer_energy[1] / cycle_difference))
			logs['weight_buffer_energy'].append((weight_buffer_energy[0] / cycle_difference, weight_buffer_energy[1] / cycle_difference))
			logs['mask_buffer_energy'].append((mask_buffer_energy[0] / cycle_difference, mask_buffer_energy[1] / cycle_difference))

			mac_lane_utilization, activation_buffer_utilization, weight_buffer_utilization, mask_buffer_utilization = get_utilization(accelerator)

			logs['activation_buffer_utilization'].append(activation_buffer_utilization)
			logs['weight_buffer_utilization'].append(weight_buffer_utilization)
			logs['mask_buffer_utilization'].append(mask_buffer_utilization)
			logs['mac_lane_utilization'].append(mac_lane_utilization)
	else:
		assert accelerator.cycle == 1

		mac_lane_utilization, activation_buffer_utilization, weight_buffer_utilization, mask_buffer_utilization = get_utilization(accelerator)

		logs = {'cycle': [accelerator.cycle], 'total_pe_energy': [total_pe_energy], 'activation_buffer_energy': [activation_buffer_energy], 'weight_buffer_energy': [weight_buffer_energy], 'mask_buffer_energy': [mask_buffer_energy], 'activation_buffer_utilization': [activation_buffer_utilization], 'weight_buffer_utilization': [weight_buffer_utilization], 'mask_buffer_utilization': [mask_buffer_utilization], 'mac_lane_utilization': [mac_lane_utilization]}

	if accelerator.cycle % plot_steps:
		json.dump(logs, open(os.path.join(logs_dir, 'logs.json'), 'w+'))

	return logs


def plot_metrics(logs, logs_dir, constants):
	fig, (ax_power, ax_utilization) = plt.subplots(2, 1)
	ax_power.plot(logs['cycle'], [pe_energy[0] * constants['clock_frequency'] for pe_energy in logs['total_pe_energy']], 'b-', label='PEs (dynamic)')
	ax_power.plot(logs['cycle'], [pe_energy[0] * constants['clock_frequency'] for pe_energy in logs['total_pe_energy']], 'b--', label='PEs (leakage)')
	ax_power.plot(logs['cycle'], [logs['activation_buffer_energy'][i][0] + logs['weight_buffer_energy'][i][0] + logs['mask_buffer_energy'][i][0] for i in range(len(logs['cycle']))], 'k-', label='Buffers (dynamic)')
	ax_power.plot(logs['cycle'], [(logs['activation_buffer_energy'][i][1] + logs['weight_buffer_energy'][i][1] + logs['mask_buffer_energy'][i][1]) for i in range(len(logs['cycle']))], 'k--', label='Bufferd (leakage)')
	ax_power.set_ylabel('Power (mW)')
	ax_power.legend(loc='upper left')

	ax_utilization.plot(logs['cycle'], [util * 100.0 for util in logs['mac_lane_utilization']], 'b-', label='MAC Lanes')
	ax_utilization.plot(logs['cycle'], [util * 100.0 for util in logs['activation_buffer_utilization']], 'k-', label='Activation Buffer')
	ax_utilization.plot(logs['cycle'], [util * 100.0 for util in logs['weight_buffer_utilization']], 'k--', label='Weight Buffer')
	ax_utilization.plot(logs['cycle'], [util * 100.0 for util in logs['mask_buffer_utilization']], color='grey', label='Mask Buffer')
	ax_utilization.set_xlabel('Cycles')
	ax_utilization.set_ylabel('Utilization (%)')
	ax_utilization.legend(loc='upper left')

	plt.savefig(os.path.join(logs_dir, 'metrics.pdf'), bbox_inches='tight')
	plt.close()


def main(model_dict: dict, config: dict, constants: dict, design_space: dict, logs_dir: str, plot_steps: int, utilization_dir: str, debug=False):
	"""Run a model_dict on an Accelerator object"""

	# Check if configuration is valid
	check_config(config, design_space)

	# Instantiate accelerator baesd on given configuration
	accelerator = Accelerator(config, constants)
	print(f'{color.GREEN}Accelerator area: {accelerator.area / 1e6 : 0.03f} mm\u00b2{color.ENDC}')
	
	# Get tiled ops from model dictionary
	memory_ops, compute_ops, num_ops = dict2ops(model_dict, config, tile_compute_ops=True, tile_memory_ops=False, debug=debug)

	assert type(memory_ops[0]) == list and type(compute_ops[0]) == list
	memory_op_idx, compute_op_idx, ops_done = (0, [0] * len(memory_ops[0])), (0, [0] * len(compute_ops[0])), 0
	memory_op, compute_op = get_op_list(memory_ops, memory_op_idx), get_op_list(compute_ops, compute_op_idx)

	# Create logs dictionary
	logs = {}

	pbar = tqdm(total=num_ops)
	pbar.set_description('Simulating accelerator')

	sp_char = "\n\t"
	reverse_memory_order = False

	# Run operations on the accelerator in a cycle-accurate manner
	while ops_done < num_ops:

		# Update progress bar
		pbar.update(ops_done - pbar.n)

		# Print cycle
		if debug: tqdm.write(f'{color.GREEN}Cycle: {accelerator.cycle + 1}{color.ENDC}')

		memory_stall, compute_stall = False, False

		if debug: 
			tqdm.write(f'{color.HEADER}Running memory operation(s) with name(s):\n\t{f"{sp_char}".join([f"- {op.op_name}" for op in memory_op])}\nand compute operation(s) with name(s):\n\t{f"{sp_char}".join([f"- {op.op_name}" for op in compute_op])}{color.ENDC}')

		# Run memory operation
		if memory_op:
			memory_stall = [False] * len(memory_op)
			debug_output = []

			# Reverse operation order to give equal priority 
			head_ids, head_ops = [], []
			for head_idx, head_op in enumerate(memory_op):
				head_ids.append(head_idx); head_ops.append(head_op)
			if reverse_memory_order:
				head_ids.reverse(); head_ops.reverse()
			reverse_memory_order = not reverse_memory_order

			for head_idx, head_op in zip(head_ids, head_ops):
				# Get corresponding data object
				data = head_op.convert_to_data()

				last_compute_done, store_op = True, False
				if isinstance(head_op, (MemoryStoreOp, MemoryStoreTiledOp)): 
					last_compute_op = get_last_compute_op(head_op, head_idx, memory_op_idx, compute_ops)
					last_compute_done = last_compute_op.done
					store_op = True

				buffer = getattr(accelerator, f'{data.data_type}_buffer')

				if buffer.ready:
					# Previous memory_op for this buffer is done
					prev_memory_op_done(head_op, head_idx, memory_op_idx, memory_ops)

					if buffer.can_store(data) and last_compute_done:
						memory_stall[head_idx] = False
				else:
					memory_stall[head_idx] = True
					if debug:
						op_debug_output = []
						if not buffer.ready: op_debug_output.append(f'{color.WARNING}Memory stall{f" for head {head_idx + 1}" if len(memory_op) > 1 else ""}: {buffer.buffer_type} buffer not ready{color.ENDC}')
						if not buffer.can_store(data): op_debug_output.append(f'{color.WARNING}Memory stall{f" for head {head_idx + 1}" if len(memory_op) > 1 else ""}: {buffer.buffer_type} buffer can\'t store data of size {data.data_size}{color.ENDC}')
						if not last_compute_done: op_debug_output.append(f'{color.WARNING}Memory stall{f" for head {head_idx + 1}" if len(memory_op) > 1 else ""}: waiting for last compute operation "{get_last_compute_op(head_op, head_idx, memory_op_idx, compute_ops).op_name}"{color.ENDC}')

					debug_output.append("\n".join(op_debug_output))
				
				if not memory_stall[head_idx]:
					if store_op:
						removed_old_data = buffer.store(data)
						removed_old_data_mask = accelerator.mask_buffer.store(data)
					else:
						removed_old_data = buffer.load(data)
						removed_old_data_mask = accelerator.mask_buffer.load(data)

			if debug: 
				if len(debug_output) > 1:
					if not reverse_memory_order: debug_output.reverse()
					tqdm.write("\n".join(debug_output))
				else:
					tqdm.write(debug_output[0])

		# Run compute operation
		if compute_op:
			compute_stall = [False] * len(compute_op)
			for head_idx, head_op in enumerate(compute_op):
				# Check if required data is in memory
				for data_name in head_op.required_in_buffer:
					if not accelerator.activation_buffer.data_in_buffer(data_name) and not accelerator.weight_buffer.data_in_buffer(data_name):
						compute_stall[head_idx] = True
						if debug: tqdm.write(f'{color.WARNING}Compute stall{f" for head {head_idx + 1}" if len(compute_op) > 1 else ""}: {data_name} required in buffer{color.ENDC}')
						break

				if not compute_stall[head_idx]:
					assigned_op = accelerator.assign_op(head_op)
					if assigned_op:
						accelerator.set_required(head_op)
					else:
						compute_stall[head_idx] = True 
						if debug: tqdm.write(f'{color.WARNING}Compute stall{f" for head {head_idx + 1}" if len(compute_op) > 1 else ""}: no MAC lanes are ready{color.ENDC}')

		# Process cycle for every module
		total_pe_energy, activation_buffer_energy, weight_buffer_energy, mask_buffer_energy = accelerator.process_cycle(memory_ops, compute_ops)
		accelerator.cycle += 1

		# Log energy values for each cycle 
		logs = log_energy(logs, total_pe_energy, activation_buffer_energy, weight_buffer_energy, mask_buffer_energy, logs_dir, accelerator, plot_steps)

		if debug:
			mac_lane_utilization, activation_buffer_utilization, weight_buffer_utilization, mask_buffer_utilization = get_utilization(accelerator)

			tqdm.write(f'Activation Buffer used: {activation_buffer_utilization * 100.0 : 0.3f}%')
			tqdm.write(f'Weight Buffer used: {weight_buffer_utilization * 100.0 : 0.3f}%')
			tqdm.write(f'Mask Buffer used: {mask_buffer_utilization * 100.0 : 0.3f}%')
			tqdm.write(f'MAC Lanes used: {mac_lane_utilization * 100.0 : 0.3f}%')

		if accelerator.cycle % plot_steps == 0:
			# Plot utilization of the accelerator
			accelerator.plot_utilization(utilization_dir)

			# Plot metrics
			plot_metrics(logs, logs_dir, constants)

		if all(memory_stall) and all(compute_stall) and accelerator.all_macs_free():
			min_cycles = min([process_cycles for process_cycles in [accelerator.activation_buffer.process_cycles, accelerator.weight_buffer.process_cycles, accelerator.mask_buffer.process_cycles] if process_cycles not in [0, None]])

			if min_cycles > 1:
				activation_buffer_energy = accelerator.activation_buffer.process_cycle(min_cycles)
				weight_buffer_energy = accelerator.weight_buffer.process_cycle(min_cycles)
				mask_buffer_energy = accelerator.mask_buffer.process_cycle(min_cycles)

				accelerator.cycle += min_cycles

				logs = log_energy(logs, (0, 0), activation_buffer_energy, weight_buffer_energy, mask_buffer_energy, logs_dir, accelerator, plot_steps)
				continue

		memory_op_idx, ops_done = update_op_idx(memory_ops, memory_op_idx, memory_stall, ops_done)
		compute_op_idx, ops_done = update_op_idx(compute_ops, compute_op_idx, compute_stall, ops_done)

		memory_op, compute_op = get_op_list(memory_ops, memory_op_idx), get_op_list(compute_ops, compute_op_idx)

	print(f'{color.GREEN}Finished simulation{color.ENDC}')
	pbar.close()


if __name__ == '__main__':
	parser = argparse.ArgumentParser(
		description='Input parameters for conversion',
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	parser.add_argument('--model_dict_path',
		metavar='',
		type=str,
		default='./model_dicts/bert_tiny.json',
		help='path where the model dictionary file is stored')
	parser.add_argument('--config_path',
		metavar='',
		type=str,
		default='./config/config.yaml',
		help='path to the accelerator configuration file')
	parser.add_argument('--constants_path',
		metavar='',
		type=str,
		default='./constants/constants.yaml',
		help='path to the accelerator constants file')
	parser.add_argument('--design_space_path',
		metavar='',
		type=str,
		default='./design_space/design_space.yaml',
		help='path to the design space file')
	parser.add_argument('--plot_steps',
		metavar='',
		type=int,
		default=1,
		help='number of cycles after which to plot')
	parser.add_argument('--logs_dir',
		metavar='',
		type=str,
		default='./logs/',
		help='directory to logs')
	parser.add_argument('--utilization_dir',
		metavar='',
		type=str,
		default='./logs/utilization/',
		help='directory to store utlization plots')
	parser.add_argument('--debug',
		dest='debug',
		help='print debugging statements',
		action='store_true')
	parser.set_defaults(debug=DEBUG)

	args = parser.parse_args()

	if os.path.exists(args.model_dict_path):
		model_dict = json.load(open(args.model_dict_path, 'r'))
	else:
		raise FileNotFoundError(f'Couldn\'t find JSON file for given path: {args.model_dict_path}')

	if os.path.exists(args.config_path):
		config = yaml.safe_load(open(args.config_path, 'r'))
	else:
		raise FileNotFoundError(f'Couldn\'t find YAML file for given path: {args.config_path}')

	if os.path.exists(args.constants_path):
		constants = yaml.safe_load(open(args.constants_path, 'r'))
	else:
		raise FileNotFoundError(f'Couldn\'t find YAML file for given path: {args.constants_path}')

	if os.path.exists(args.design_space_path):
		design_space = yaml.safe_load(open(args.design_space_path, 'r'))
	else:
		raise FileNotFoundError(f'Couldn\'t find YAML file for given path: {args.design_space_path}')

	if args.debug and OVERWRITE_PLOT_STEPS_WITH_DEBUG: 
		args.plot_steps = 1
		print(f'{color.WARNING}Plotting steps set to 1 in debugging mode{color.ENDC}')

	if os.path.exists(args.logs_dir) and OVERWRITE_LOGS: shutil.rmtree(args.logs_dir)
	if os.path.exists(args.utilization_dir) and OVERWRITE_LOGS: shutil.rmtree(args.utilization_dir)
	os.makedirs(args.logs_dir)

	main(model_dict, config, constants, design_space, args.logs_dir, args.plot_steps, args.utilization_dir, args.debug)

