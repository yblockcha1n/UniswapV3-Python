from web3 import Web3
from eth_typing import Address
import json
import configparser
import os
from pathlib import Path
from typing import Dict, Any, Tuple
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class UniswapV3Swap:
    def __init__(self):
        self.config = self._load_config()
        
        self.w3 = Web3(Web3.HTTPProvider(self.config['ethereum']['infura_url']))
        if not self.w3.is_connected():
            raise ConnectionError("Failed to Connect to Ethereum Network")
            
        self.account = self.w3.eth.account.from_key(self.config['ethereum']['private_key'])
        logger.info(f"Connected to Network: {self.w3.eth.chain_id}")
        
        self.swap_router_address = Web3.to_checksum_address(self.config['contracts']['swap_router'])
        self.quoter_address = Web3.to_checksum_address(self.config['contracts']['quoter'])
        
        self.swap_router_abi = self._load_abi('UniswapV3SwapRouter.json')
        self.quoter_abi = self._load_abi('UniswapV3Quoter.json')
        self.erc20_abi = self._load_abi('ERC20Standard.json')
        
        self.swap_router = self.w3.eth.contract(
            address=self.swap_router_address,
            abi=self.swap_router_abi
        )
        self.quoter = self.w3.eth.contract(
            address=self.quoter_address,
            abi=self.quoter_abi
        )

    @staticmethod
    def _load_config() -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config_path = Path(__file__).parent.parent / 'config' / 'config.ini'
        if not config_path.exists():
            raise FileNotFoundError(f"Config File Not Found: {config_path}")
        config.read(config_path)
        return config

    @staticmethod
    def _load_abi(filename: str) -> list:
        abi_path = Path(__file__).parent.parent / 'abi' / filename
        if not abi_path.exists():
            raise FileNotFoundError(f"ABI File Not Found: {abi_path}")
        with open(abi_path, 'r') as f:
            loaded_json = json.load(f)
            
            # ABIが直接配列として保存されている場合とオブジェクトのabiプロパティとして保存されている場合の両方に対応 (ベタ張り可)
            if isinstance(loaded_json, list):
                return loaded_json
            elif isinstance(loaded_json, dict) and 'abi' in loaded_json:
                return loaded_json['abi']
            else:
                raise ValueError(f"Invalid ABI format in {filename}")

    def check_token_balance(self, token_address: str, address: str = None) -> int:
        if address is None:
            address = self.account.address
            
        token_address = Web3.to_checksum_address(token_address)
        
        token_contract = self.w3.eth.contract(
            address=token_address,
            abi=self.erc20_abi
        )
        balance = token_contract.functions.balanceOf(address).call()
        symbol = token_contract.functions.symbol().call()
        decimals = token_contract.functions.decimals().call()
        
        logger.info(f"Balance of {symbol}: {balance / 10**decimals}")
        return balance

    def check_allowance(self, token_address: str, owner: str = None) -> int:
        if owner is None:
            owner = self.account.address
            
        token_address = Web3.to_checksum_address(token_address)
        
        token_contract = self.w3.eth.contract(
            address=token_address,
            abi=self.erc20_abi
        )
        allowance = token_contract.functions.allowance(
            owner,
            self.swap_router_address
        ).call()
        
        symbol = token_contract.functions.symbol().call()
        decimals = token_contract.functions.decimals().call()
        logger.info(f"Allowance of {symbol}: {allowance / 10**decimals}")
        return allowance

    def get_quote(self, token_in: str, token_out: str, amount_in: int) -> int:
            try:
                token_in = Web3.to_checksum_address(token_in)
                token_out = Web3.to_checksum_address(token_out)
                
                # パラメータを構造体として渡す
                params = {
                    'tokenIn': token_in,
                    'tokenOut': token_out,
                    'amountIn': amount_in,
                    'fee': int(self.config['swap_settings']['fee_tier']),
                    'sqrtPriceLimitX96': 0
                }
                
                quote = self.quoter.functions.quoteExactInputSingle(
                    (
                        params['tokenIn'],
                        params['tokenOut'],
                        params['amountIn'],
                        params['fee'],
                        params['sqrtPriceLimitX96']
                    )
                ).call()
                logger.info(f"Quote received: {quote}")
                return quote
            except Exception as e:
                logger.error(f"Error getting quote: {str(e)}")
                raise

    def approve_token(self, token_address: str, amount: int) -> dict:
            token_address = Web3.to_checksum_address(token_address)
            
            token_contract = self.w3.eth.contract(
                address=token_address,
                abi=self.erc20_abi
            )
            
            try:
                current_allowance = self.check_allowance(token_address)
                if current_allowance >= amount:
                    logger.info("Sufficient Allowance already exists")
                    return None
                    
                approve_tx = token_contract.functions.approve(
                    self.swap_router_address,
                    amount
                ).build_transaction({
                    'from': self.account.address,
                    'nonce': self.w3.eth.get_transaction_count(self.account.address),
                    'gas': 250000,
                    'gasPrice': self.w3.eth.gas_price
                })
                
                signed_approve_tx = self.account.sign_transaction(approve_tx)
                approve_tx_hash = self.w3.eth.send_raw_transaction(signed_approve_tx.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(approve_tx_hash)
                logger.info(f"Approval TX Successful: {approve_tx_hash.hex()}")
                return receipt
            except Exception as e:
                logger.error(f"Error in Token Approval: {str(e)}")
                raise

    def swap_exact_input_single(self, 
                                token_in: str,
                                token_out: str,
                                amount_in: int) -> dict:
            try:
                token_in = Web3.to_checksum_address(token_in)
                token_out = Web3.to_checksum_address(token_out)
                
                balance = self.check_token_balance(token_in)
                if balance < amount_in:
                    raise ValueError(f"Insufficient Token Balance: {balance} < {amount_in}")
                
                approval_receipt = self.approve_token(token_in, amount_in)
                
                deadline = self.w3.eth.get_block('latest')['timestamp'] + \
                        int(self.config['swap_settings']['deadline_minutes']) * 60

                fee_tier = int(self.config['swap_settings']['fee_tier'])
                logger.info(f"Using Fee Tier: {fee_tier}")

                params = {
                    "tokenIn": token_in,
                    "tokenOut": token_out,
                    "fee": fee_tier,
                    "recipient": self.account.address,
                    "deadline": deadline,
                    "amountIn": amount_in,
                    "amountOutMinimum": 0,
                    "sqrtPriceLimitX96": 0
                }

                swap_tx = self.swap_router.functions.exactInputSingle(params).build_transaction({
                    'from': self.account.address,
                    'nonce': self.w3.eth.get_transaction_count(self.account.address),
                    'gas': 500000,
                    'gasPrice': self.w3.eth.gas_price,
                })

                signed_swap_tx = self.account.sign_transaction(swap_tx)
                swap_tx_hash = self.w3.eth.send_raw_transaction(signed_swap_tx.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(swap_tx_hash)
                
                if receipt['status'] == 1:
                    logger.info(f"SwapTX Successful: {swap_tx_hash.hex()}")
                    self.check_token_balance(token_in)
                    self.check_token_balance(token_out)
                else:
                    logger.error("SwapTX Failed!")
                    logger.error(f"TX Receipt: {receipt}")
                
                return receipt
            except Exception as e:
                logger.error(f"Error in SwapTX: {str(e)}")
                raise

def main():
    try:
        uniswap = UniswapV3Swap()
        config = uniswap.config
        
        amount_in = Web3.to_wei(0.1, 'ether')
        token_in = config['tokens']['token_a']
        token_out = config['tokens']['token_b']
        
        uniswap.check_token_balance(token_in)
        
        quote = uniswap.get_quote(token_in, token_out, amount_in)
        print(f"Expected output: {Web3.from_wei(quote, 'ether')} TokenB")
        
        receipt = uniswap.swap_exact_input_single(
            token_in,
            token_out,
            amount_in
        )
        
        if receipt['status'] == 1:
            print("Swap Successful!")
            print(f"TX Hash: {receipt['transactionHash'].hex()}")
            uniswap.check_token_balance(token_in)
            uniswap.check_token_balance(token_out)
        else:
            print("Swap Failed!")
            print(f"Transaction hash: {receipt['transactionHash'].hex()}")
            
    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}")
        raise

if __name__ == "__main__":
    main()