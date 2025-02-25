import time
import argparse
import requests
import bittensor as bt
import logging
from types import SimpleNamespace
from collections import Counter

# Configure logging with a standard format.
logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more detailed output.
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_config():
    """
    Parse command-line arguments and return a configuration object.
    """
    parser = argparse.ArgumentParser('Lolocaust')
    parser.add_argument('--netuid', type=int, default=28, help='The netuid to query events for')
    parser.add_argument('--api_key', type=str, required=True, help='API key for taostats.io')
    bt.wallet.add_args(parser)
    bt.subtensor.add_args(parser)
    return bt.config(parser)

def events(config, coldkey):
    """
    Retrieve delegation events for a given coldkey from the taostats API.
    
    Args:
        config: Configuration object containing API key, netuid, etc.
        coldkey (str): The miner's SS58 address.
    
    Returns:
        List of SimpleNamespace objects representing the events.
    """
    url = 'https://api.taostats.io/api/delegation/v1'
    params = {
        'nominator': coldkey,
        'netuid': config.netuid,
        'action': 'all',
        'page': 1,
        'limit': 200
    }
    headers = {
        'Authorization': config.api_key,
        'accept': 'application/json'
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()  # Raise exception for HTTP errors.
        json_data = response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching events for coldkey {coldkey}: {e}")
        return []  # Return empty list on error.
    except ValueError as e:
        logger.error(f"Error decoding JSON for coldkey {coldkey}: {e}")
        return []
    
    events_list = []
    json_events = json_data.get('data', [])
    for event in json_events:
        try:
            # Extract and process event details.
            block = int(event.get('block_number', 0))
            timestamp = event.get('timestamp', '')
            action_raw = event.get('action', '')
            
            # Map API actions to our own labels.
            # "UNDELEGATE" corresponds to an unstake event.
            if action_raw == "UNDELEGATE":
                action = 'sell'
            elif action_raw == "DELEGATE":
                action = 'buy'
            else:
                continue  # Skip unknown actions.

            nominator = event.get('nominator', {})
            delegate = event.get('delegate', {})
            coldkey_val = nominator.get('ss58', '')
            hotkey = delegate.get('ss58', '')
            # Convert the amount to tao (assuming the API amount is in a smaller denomination).
            amount = float(event.get('amount', 0)) / 1e10
            price = float(event.get('alpha_price_in_tao', 0))
            
            events_list.append(SimpleNamespace(
                block=block,
                timestamp=timestamp,
                action=action,
                coldkey=coldkey_val,
                hotkey=hotkey,
                amount=amount,
                price=price
            ))
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Error processing event data: {e}. Event data: {event}")
            continue

    return events_list

def compute_unstake_value(config, coldkey, start_block):
    """
    Compute the total unstaked value (in tao) for a given coldkey over blocks 
    at or after start_block, considering only unstake events.
    
    Args:
        config: Configuration object.
        coldkey (str): The miner's SS58 address.
        start_block (int): Only consider events from this block onward.
    
    Returns:
        float: The total unstaked value.
    """
    events_list = events(config, coldkey)
    total_value = 0.0
    for event in events_list:
        # Only consider events since start_block and only unstake events.
        if event.block >= start_block and event.action == 'sell':
            try:
                total_value += event.amount * event.price
            except Exception as e:
                logger.error(f"Error computing value for event at block {event.block}: {e}")
                continue
    return total_value

def scores_to_weights(scores):
    """
    Convert raw miner scores to normalized weights.

    For each miner:
      - If the miner's raw score is positive (indicating unstake events), that score is used.
      - If the miner has no unstake events (score == 0) or negative values, the score is set to zero.
    
    After adjusting the scores, the function normalizes them so that:
      - The miner with the highest positive score gets the highest weight.
      - The miner with the lowest positive score gets the lowest positive weight.
      - All weights sum to 1.
      - Miners with no unstake events receive a weight of 0.
    
    Args:
        scores (list of float): List of raw scores for each miner.
    
    Returns:
        list of float: Normalized weights that sum to 1.
    """
    # Zero out negative values and retain positive scores.
    adjusted_scores = [score if score > 0 else 0 for score in scores]
    total = sum(adjusted_scores)
    if total > 0:
        weights = [score / total for score in adjusted_scores]
    else:
        weights = [0] * len(scores)
    return weights

def main(config):
    """
    Main loop to periodically compute and update miner weights based on unstake events.

    New algorithm:
      1. Retrieve the metagraph which contains the property 'last_step' – the block when incentives were last distributed.
      2. Only run the computation if current_block - metagraph.last_step > 360.
      3. For each unique coldkey, compute the total unstake (sell) value from events that occurred since metagraph.last_step.
      4. Normalize these scores so that the sum of weights is 1.
      5. Project the unique weights onto a vector of length metagraph.uids such that if a coldkey appears in multiple positions,
         its weight is evenly distributed across those positions.
      6. After updating weights, update metagraph.last_step to the current block (simulating incentive distribution).
    """
    wallet = bt.wallet(config=config)
    logger.info("Starting main loop for incentive-based weight updates.")

    while True:
        try:
            sub = bt.subtensor(config=config)
            logger.info(f"Subtensor: {sub}")

            metagraph = sub.metagraph(config.netuid)
            logger.info(f"Metagraph retrieved. Total miners (uids): {len(metagraph.uids)}")

            current_block = sub.get_current_block()
            logger.info(f"Current block: {current_block}")

            # Run when we are in the range of the last blocks of the tempo (with two block leeway)
            if metagraph.blocks_since_last_step >= (metagraph.tempo - 2):
                logger.info(f"More than {metagraph.tempo} blocks since last_step ({metagraph.last_step}). Computing new weights.")
                start_block = metagraph.last_step  # Only consider events since the last incentive distribution.
                
                # Build dictionary for unique coldkeys with their computed unstake score.
                unique_scores = {}
                unique_coldkeys = set(metagraph.coldkeys)
                for cold in unique_coldkeys:
                    try:
                        score = compute_unstake_value(config, cold, start_block)
                        unique_scores[cold] = score
                        logger.info(f"Unstake score for miner (coldkey {cold}) since block {start_block}: {score}")
                    except Exception as e:
                        logger.error(f"Error computing score for miner {cold}: {e}")
                        unique_scores[cold] = 0.0

                # Normalize the unique scores to generate weights.
                unique_keys = list(unique_scores.keys())
                unique_scores_list = [unique_scores[key] for key in unique_keys]
                normalized_weights_list = scores_to_weights(unique_scores_list)
                unique_weights = dict(zip(unique_keys, normalized_weights_list))
                logger.info(f"Normalized unique weights: {unique_weights}")

                # Project the unique weights onto a final weight vector corresponding to metagraph.uids.
                coldkey_counts = Counter(metagraph.coldkeys)
                final_weights = []
                for cold in metagraph.coldkeys:
                    # Distribute the unique weight equally among all positions for that coldkey.
                    weight = unique_weights.get(cold, 0) / coldkey_counts[cold]
                    final_weights.append(weight)
                logger.info(f"Final weights vector (projected): {final_weights}")

                # Update weights for all miners.
                try:
                    sub.set_weights(
                        netuid=config.netuid,
                        wallet=wallet,
                        uids=metagraph.uids.tolist(),
                        weights=final_weights,
                    )
                    logger.info("Weights updated based on unstake events since last incentive distribution.")
                except Exception as e:
                    logger.error(f"Error updating weights: {e}")

                # Update metagraph.last_step to current_block to mark this distribution.
                # (In practice, this should be updated on-chain; here we simulate it.)
                metagraph.last_step = current_block
                logger.info(f"Updated metagraph.last_step to {current_block}.")
            else:
                logger.debug("Not enough blocks have passed since last incentive distribution. Skipping update.")
            
            # Wait for the next block update before recalculating.
            sub.wait_for_block()
                
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            sub.wait_for_block()

if __name__ == "__main__":
    try:
        config = get_config()
        main(config)
    except Exception as e:
        logger.critical(f"Fatal error during startup: {e}")
        raise
