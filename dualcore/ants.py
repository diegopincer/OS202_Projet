"""
Module managing an ant colony in a labyrinth
"""
import numpy as np
import maze
import pheromone
import direction as d
import pygame as pg
from mpi4py import MPI

comm = MPI.COMM_WORLD.Dup()
rank = comm.Get_rank()  # Get the rank of the current process
nbp = comm.Get_size()   # Get the total number of processes

UNLOADED, LOADED = False, True

exploration_coefs = 0.


class Colony:
    """
    Represent an ant colony. Ants are not individualized for performance reasons!

    Inputs :
        nb_ants  : Number of ants in the anthill
        pos_init : Initial positions of ants (anthill position)
        max_life : Maximum life that ants can reach
    """
    def __init__(self, nb_ants, pos_init, max_life):
        # Each ant has is own unique random seed
        self.seeds = np.arange(1, nb_ants+1, dtype=np.int64)
        # State of each ant : loaded or unloaded
        self.is_loaded = np.zeros(nb_ants, dtype=np.int8)
        # Compute the maximal life amount for each ant :
        #   Updating the random seed :
        self.seeds[:] = np.mod(16807*self.seeds[:], 2147483647)
        # Amount of life for each ant = 75% à 100% of maximal ants life
        self.max_life = max_life * np.ones(nb_ants, dtype=np.int32)
        self.max_life -= np.int32(max_life*(self.seeds/2147483647.))//4
        # Ages of ants : zero at beginning
        self.age = np.zeros(nb_ants, dtype=np.int64)
        # History of the path taken by each ant. The position at the ant's age represents its current position.
        self.historic_path = np.zeros((nb_ants, max_life+1, 2), dtype=np.int16)
        self.historic_path[:, 0, 0] = pos_init[0]
        self.historic_path[:, 0, 1] = pos_init[1]
        # Direction in which the ant is currently facing (depends on the direction it came from).
        self.directions = d.DIR_NONE*np.ones(nb_ants, dtype=np.int8)


    def return_to_nest(self, loaded_ants, pos_nest, food_counter):
        """
        Function that returns the ants carrying food to their nests.

        Inputs :
            loaded_ants: Indices of ants carrying food
            pos_nest: Position of the nest where ants should go
            food_counter: Current quantity of food in the nest

        Returns the new quantity of food
        """
        self.age[loaded_ants] -= 1

        in_nest_tmp = self.historic_path[loaded_ants, self.age[loaded_ants], :] == pos_nest
        if in_nest_tmp.any():
            in_nest_loc = np.nonzero(np.logical_and(in_nest_tmp[:, 0], in_nest_tmp[:, 1]))[0]
            if in_nest_loc.shape[0] > 0:
                in_nest = loaded_ants[in_nest_loc]
                self.is_loaded[in_nest] = UNLOADED
                self.age[in_nest] = 0
                food_counter += in_nest_loc.shape[0]
        return food_counter

    def explore(self, unloaded_ants, the_maze, pos_food, pos_nest, pheromones):
        """
        Management of unloaded ants exploring the maze.

        Inputs:
            unloadedAnts: Indices of ants that are not loaded
            maze        : The maze in which ants move
            posFood     : Position of food in the maze
            posNest     : Position of the ants' nest in the maze
            pheromones  : The pheromone map (which also has ghost cells for
                          easier edge management)

        Outputs: None
        """
        # Update of the random seed (for manual pseudo-random) applied to all unloaded ants
        self.seeds[unloaded_ants] = np.mod(16807*self.seeds[unloaded_ants], 2147483647)

        # Calculating possible exits for each ant in the maze:
        old_pos_ants = self.historic_path[range(0, self.seeds.shape[0]), self.age[:], :]
        has_north_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.NORTH) > 0
        has_east_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.EAST) > 0
        has_south_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.SOUTH) > 0
        has_west_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.WEST) > 0

        # Reading neighboring pheromones:
        north_pos = np.copy(old_pos_ants)
        north_pos[:, 1] += 1
        north_pheromone = pheromones.pheromon[north_pos[:, 0], north_pos[:, 1]]*has_north_exit

        east_pos = np.copy(old_pos_ants)
        east_pos[:, 0] += 1
        east_pos[:, 1] += 2
        east_pheromone = pheromones.pheromon[east_pos[:, 0], east_pos[:, 1]]*has_east_exit

        south_pos = np.copy(old_pos_ants)
        south_pos[:, 0] += 2
        south_pos[:, 1] += 1
        south_pheromone = pheromones.pheromon[south_pos[:, 0], south_pos[:, 1]]*has_south_exit

        west_pos = np.copy(old_pos_ants)
        west_pos[:, 0] += 1
        west_pheromone = pheromones.pheromon[west_pos[:, 0], west_pos[:, 1]]*has_west_exit

        max_pheromones = np.maximum(north_pheromone, east_pheromone)
        max_pheromones = np.maximum(max_pheromones, south_pheromone)
        max_pheromones = np.maximum(max_pheromones, west_pheromone)

        # Calculating choices for all ants not carrying food (for others, we calculate but it doesn't matter)
        choices = self.seeds[:] / 2147483647.

        # Ants explore the maze by choice or if no pheromone can guide them:
        ind_exploring_ants = np.nonzero(
            np.logical_or(choices[unloaded_ants] <= exploration_coefs, max_pheromones[unloaded_ants] == 0.))[0]
        if ind_exploring_ants.shape[0] > 0:
            ind_exploring_ants = unloaded_ants[ind_exploring_ants]
            valid_moves = np.zeros(choices.shape[0], np.int8)
            nb_exits = has_north_exit * np.ones(has_north_exit.shape) + has_east_exit * np.ones(has_east_exit.shape) + \
                has_south_exit * np.ones(has_south_exit.shape) + has_west_exit * np.ones(has_west_exit.shape)
            while np.any(valid_moves[ind_exploring_ants] == 0):
                # Calculating indices of ants whose last move was not valid:
                ind_ants_to_move = ind_exploring_ants[valid_moves[ind_exploring_ants] == 0]
                self.seeds[:] = np.mod(16807*self.seeds[:], 2147483647)
                # Choosing a random direction:
                dir = np.mod(self.seeds[ind_ants_to_move], 4)
                old_pos = self.historic_path[ind_ants_to_move, self.age[ind_ants_to_move], :]
                new_pos = np.copy(old_pos)
                new_pos[:, 1] -= np.logical_and(dir == d.DIR_WEST,
                                                has_west_exit[ind_ants_to_move]) * np.ones(new_pos.shape[0], dtype=np.int16)
                new_pos[:, 1] += np.logical_and(dir == d.DIR_EAST,
                                                has_east_exit[ind_ants_to_move]) * np.ones(new_pos.shape[0], dtype=np.int16)
                new_pos[:, 0] -= np.logical_and(dir == d.DIR_NORTH,
                                                has_north_exit[ind_ants_to_move]) * np.ones(new_pos.shape[0], dtype=np.int16)
                new_pos[:, 0] += np.logical_and(dir == d.DIR_SOUTH,
                                                has_south_exit[ind_ants_to_move]) * np.ones(new_pos.shape[0], dtype=np.int16)
                # Valid move if we didn't stay in place due to a wall
                valid_moves[ind_ants_to_move] = np.logical_or(new_pos[:, 0] != old_pos[:, 0], new_pos[:, 1] != old_pos[:, 1])
                # and if we're not in the opposite direction of the previous move (and if there are other exits)
                valid_moves[ind_ants_to_move] = np.logical_and(
                    valid_moves[ind_ants_to_move],
                    np.logical_or(dir != 3-self.directions[ind_ants_to_move], nb_exits[ind_ants_to_move] == 1))
                # Calculating indices of ants whose move we just validated:
                ind_valid_moves = ind_ants_to_move[np.nonzero(valid_moves[ind_ants_to_move])[0]]
                # For these ants, we update their positions and directions
                self.historic_path[ind_valid_moves, self.age[ind_valid_moves] + 1, :] = new_pos[valid_moves[ind_ants_to_move] == 1, :]
                self.directions[ind_valid_moves] = dir[valid_moves[ind_ants_to_move] == 1]

        ind_following_ants = np.nonzero(np.logical_and(choices[unloaded_ants] > exploration_coefs,
                                                       max_pheromones[unloaded_ants] > 0.))[0]
        if ind_following_ants.shape[0] > 0:
            ind_following_ants = unloaded_ants[ind_following_ants]
            self.historic_path[ind_following_ants, self.age[ind_following_ants] + 1, :] = \
                self.historic_path[ind_following_ants, self.age[ind_following_ants], :]
            max_east = (east_pheromone[ind_following_ants] == max_pheromones[ind_following_ants])
            self.historic_path[ind_following_ants, self.age[ind_following_ants]+1, 1] += \
                max_east * np.ones(ind_following_ants.shape[0], dtype=np.int16)
            max_west = (west_pheromone[ind_following_ants] == max_pheromones[ind_following_ants])
            self.historic_path[ind_following_ants, self.age[ind_following_ants]+1, 1] -= \
                max_west * np.ones(ind_following_ants.shape[0], dtype=np.int16)
            max_north = (north_pheromone[ind_following_ants] == max_pheromones[ind_following_ants])
            self.historic_path[ind_following_ants, self.age[ind_following_ants]+1, 0] -= max_north * np.ones(ind_following_ants.shape[0], dtype=np.int16)
            max_south = (south_pheromone[ind_following_ants] == max_pheromones[ind_following_ants])
            self.historic_path[ind_following_ants, self.age[ind_following_ants]+1, 0] += max_south * np.ones(ind_following_ants.shape[0], dtype=np.int16)

        # Aging one unit for the age of ants not carrying food
        if unloaded_ants.shape[0] > 0:
            self.age[unloaded_ants] += 1

        # Killing ants at the end of their life:
        ind_dying_ants = np.nonzero(self.age == self.max_life)[0]
        if ind_dying_ants.shape[0] > 0:
            self.age[ind_dying_ants] = 0
            self.historic_path[ind_dying_ants, 0, 0] = pos_nest[0]
            self.historic_path[ind_dying_ants, 0, 1] = pos_nest[1]
            self.directions[ind_dying_ants] = d.DIR_NONE

        # For ants reaching food, we update their states:
        ants_at_food_loc = np.nonzero(np.logical_and(self.historic_path[unloaded_ants, self.age[unloaded_ants], 0] == pos_food[0],
                                                     self.historic_path[unloaded_ants, self.age[unloaded_ants], 1] == pos_food[1]))[0]
        if ants_at_food_loc.shape[0] > 0:
            ants_at_food = unloaded_ants[ants_at_food_loc]
            self.is_loaded[ants_at_food] = True

    def advance(self, the_maze, pos_food, pos_nest, pheromones, food_counter=0):
        loaded_ants = np.nonzero(self.is_loaded == True)[0]
        unloaded_ants = np.nonzero(self.is_loaded == False)[0]
        if loaded_ants.shape[0] > 0:
            food_counter = self.return_to_nest(loaded_ants, pos_nest, food_counter)
        if unloaded_ants.shape[0] > 0:
            self.explore(unloaded_ants, the_maze, pos_food, pos_nest, pheromones)

        old_pos_ants = self.historic_path[range(0, self.seeds.shape[0]), self.age[:], :]
        has_north_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.NORTH) > 0
        has_east_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.EAST) > 0
        has_south_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.SOUTH) > 0
        has_west_exit = np.bitwise_and(the_maze.maze[old_pos_ants[:, 0], old_pos_ants[:, 1]], maze.WEST) > 0
        # Marking pheromones:
        [pheromones.mark(self.historic_path[i, self.age[i], :],
                         [has_north_exit[i], has_east_exit[i], has_west_exit[i], has_south_exit[i]]) for i in range(self.directions.shape[0])]
        
        return food_counter
    
    
        # Definition of a method to return specific attributes of the Ants class
    def returns(self):
        return self.directions, self.historic_path, self.age

# Definition of a class responsible for displaying ant sprites
class Colony_show:
    def __init__(self):
        # Initialize a list to store ant sprites
        self.sprites = []
        # Load the ant sprites from the image file "ants.png"
        img = pg.image.load("ants.png").convert_alpha()
        # Extract individual sprites from the loaded image and store them in the list
        for i in range(0, 32, 8):
            self.sprites.append(pg.Surface.subsurface(img, i, 0, 8, 8))

    # Method to display ant sprites on the screen
    def display(self, screen, directions_recv, historic_path_recv, age_recv):
        # Iterate through the received data to display ants on the screen
        [screen.blit(self.sprites[directions_recv[i]], (8*historic_path_recv[i, age_recv[i], 1], 8*historic_path_recv[i, age_recv[i], 0])) for i in range(directions_recv.shape[0])]


if __name__ == "__main__":

    import sys
    import time

    size_laby = 25, 25
    if len(sys.argv) > 2:
        size_laby = int(sys.argv[1]),int(sys.argv[2])

    resolution = size_laby[1]*8, size_laby[0]*8
    pos_food = size_laby[0]-1, size_laby[1]-1
    pos_nest = 0, 0
    max_life = 500
    

    # Check if the current process rank is 0
    if rank == 0:
        # Initialize pygame and set screen resolution
        pg.init()
        screen = pg.display.set_mode(resolution)

        # Check if command line arguments specify maximum ant life
        if len(sys.argv) > 3:
            max_life = int(sys.argv[3])
        
        # Receive maze data from process rank 1
        maze_recv = comm.recv(source=1, tag=8)
        # Initialize Maze_show object for displaying maze
        a_maze_show = maze.Maze_show(maze_recv)
        # Display maze and set snapshot flag to False
        mazeImg = a_maze_show.display()
        snapshop_taken = False

        # Receive ant directions, historic paths, and ages from process rank 1
        directions_recv = comm.recv(source=1, tag=1)
        historic_path_recv = comm.recv(source=1, tag=2)
        age_recv = comm.recv(source=1, tag=3)
        
        # Initialize Colony_show object for displaying ants
        ants_show = Colony_show()
        # Receive pheromone data from process rank 1
        pherom_recv = comm.recv(source=1, tag=4)
        # Initialize Pheromon_show object for displaying pheromones
        pherom_show = pheromone.Pheromon_show(pherom_recv)
        # Initialize frame per second list and counter
        fps=[]
        counter = 0

    # Check if the current process rank is greater than 0
    if rank > 0:
        # Initialize food counter
        food_counter = 0
        # Initialize Maze object for ant simulation
        a_maze = maze.Maze(size_laby, 12345)
        # Send maze data to process rank 0
        maze_send = a_maze.retorno()
        comm.send(maze_send, dest=0, tag=8)
        # Initialize alpha and beta parameters for pheromone calculation
        alpha = 0.9
        beta  = 0.99
        # Check if command line arguments specify alpha and beta values
        if len(sys.argv) > 4:
            alpha = float(sys.argv[4])
        if len(sys.argv) > 5:
            beta = float(sys.argv[5])
        # Initialize Pheromon object for pheromone management
        pherom = pheromone.Pheromon(size_laby, pos_food, alpha, beta)
        # Retrieve pheromone data
        pheromon_send=pherom.pheromon

        # Calculate the number of ants
        nb_ants = size_laby[0]*size_laby[1]//4
        # Initialize ant colony with nest position and maximum life
        ants = Colony(nb_ants, pos_nest, max_life)
        # Initialize unloaded ants
        unloaded_ants = np.array(range(nb_ants))

        # Send ant directions, historic paths, ages, and pheromone data to process rank 0
        comm.send(ants.directions, dest=0, tag=1)
        comm.send(ants.historic_path, dest=0, tag=2)
        comm.send(ants.age, dest=0, tag=3)
        comm.send(pheromon_send, dest=0, tag=4)
while True:
    # If the process rank is 0
    if rank == 0:
        # Check for pygame events
        for event in pg.event.get():
            if event.type == pg.QUIT:
                pg.quit()
                exit(0)

        # Record current time
        deb = time.time()

        # Receive pheromone data from process 1 and display it
        pheromon_recv = comm.recv(source=1, tag=7)
        pherom_show.display(screen, pheromon_recv)

        # Display maze image
        screen.blit(mazeImg, (0, 0))

        # Receive ant directions, historic paths, and ages from process 1 and display them
        directions_recv, historic_path_recv, age_recv = comm.recv(source=1, tag=6)
        ants_show.display(screen, directions_recv, historic_path_recv, age_recv)  

        # Update the display
        pg.display.update()

        # Receive food counter from process 1
        food_counter = comm.recv(source=1, tag=5)

        # Record end time and save snapshot if food_counter is 1 and snapshot has not been taken
        end = time.time()
        if food_counter == 1 and not snapshop_taken:
            pg.image.save(screen, "MyFirstFood.png")
            snapshop_taken = True

        # Calculate FPS and print statistics
        fps.append(int(1./(end-deb)))
        counter += 1
        print(f"FPS : {1./(end-deb):6.2f}, nourriture : {food_counter:7d}, Moyenne FPS: {np.sum(fps)/counter}", end='\r')

    # If the process rank is greater than 0
    if rank > 0:
        # Advance ants in the maze and update food counter
        food_counter = ants.advance(a_maze, pos_food, pos_nest, pherom, food_counter)
        mise_a_jour = ants.returns()

        # Retrieve updated pheromone data and send it to process 0
        pheromon_send = pherom.return_pheromon()
        comm.send(pheromon_send, dest=0, tag=7)

        # Send ant updates and food counter to process 0
        comm.send(mise_a_jour, dest=0, tag=6)
        comm.send(food_counter, dest=0, tag=5)

        # Perform pheromone evaporation around the food source
        pherom.do_evaporation(pos_food)
