import time
import argparse
import requests
import bittensor as bt
from types import SimpleNamespace


def get_config():
    parser = argparse.ArgumentParser('Lolocaust')
    parser.add_argument('--netuid', type=int, default=18, help='The netuid to query events for')
    parser.add_argument('--api_key', type=str, required=True, help='API key for taostats.io')
    parser.add_argument('--tempo', type=int, default = 360, required=False, help='Subnet tempo')
    bt.wallet.add_args( parser )
    bt.subtensor.add_args( parser )
    return bt.config( parser )

def events(config, coldkey):
    response = requests.get(
        'https://api.taostats.io/api/delegation/v1',
        params={
            'nominator': coldkey,
            'netuid': config.netuid,
            'action': 'all',
            'page': 1,
            'limit': 200
        },
        headers={
            'Authorization': config.api_key,
            'accept': 'application/json'
        }
    )
    
    events_list = []
    json_events = response.json().get('data', [])
    for event in json_events:
        block = event['block_number']
        timestamp = event['timestamp']
        
        # Map API actions to our own labels.
        if event['action'] == "UNDELEGATE":
            action = 'sell'
        elif event['action'] == "DELEGATE":
            action = 'buy'
        else:
            continue

        coldkey_val = event['nominator']['ss58']
        hotkey = event['delegate']['ss58']
        # Convert amount to tao (assuming the API amount is in a smaller denomination)
        amount = float(event['amount']) / 1e10  # Adjusted divisor for clarity
        price = float(event['alpha_price_in_tao'])
        
        events_list.append(SimpleNamespace(
            block=block,
            timestamp=timestamp,
            action=action,
            coldkey=coldkey_val,
            hotkey=hotkey,
            amount=amount,
            price=price
        ))
    return events_list

def compute_sell_value(config, coldkey, start_block):
    """
    Compute the total sold value (in tao) for a given coldkey
    over blocks at or after start_block.
    
    Args:
        coldkey (str): The miner's SS58 address.
        start_block (int): Only consider events from this block onward.
    
    Returns:
        float: The sum of (amount * price) for all sell events.
    """
    events_list = events(config, coldkey)
    total_sell_value = 0.0
    for event in events_list:
        if event.block >= start_block and event.action == 'sell':
            total_sell_value += event.amount * event.price
    return total_sell_value
    

def main( config ):
    wallet = bt.wallet( config )    
    while True:
        sub = bt.subtensor( config )
        metagraph = sub.metagraph( config.netuid )
        current_block = sub.get_current_block()
        # Check if we are at a block that is a multiple of 1000.
        if current_block % config.tempo == 0:
            start_block = current_block - config.tempo
            scores = []
            for cold in metagraph.coldkeys:
                score = compute_sell_value(cold, start_block)
                scores.append(score)
                print(f"Score for miner (coldkey {cold}): {score}")
            
            wallet = bt.wallet(name='val')
            sub.set_weights(
                netuid=config.netuid,
                wallet=wallet,
                uids=metagraph.uids.tolist(),
                weights=scores,
            )
            print("Weights updated based on sells over the last 1000 blocks.")
            # Pause slightly to avoid duplicate runs on the same block.
            time.sleep(10)
        else:
            # Check again shortly if we're not at a 1000-block boundary.
            time.sleep(1)
        
    
if __name__ == "__main__":
    main( get_config() )
    
